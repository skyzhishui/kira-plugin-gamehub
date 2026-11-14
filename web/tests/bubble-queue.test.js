/* bubble-queue.test.js — 座位发言气泡排队播放器（node:test，零依赖，假时钟）。
 *
 * 需求锁定（人设座位与陪练/进阶对手座位同规则）：
 * 1) 多条消息按到达顺序逐条展示：不合并、不取最新；
 * 2) 每条至少 1s（min interval），无排队时最多 3s 自动收起；
 * 3) 有排队时满 1s 立即换下一条（不空等、总时长不超过 3s/条）；
 * 4) 历史首载只播种不播放；DB id 去重；相同内容不同 id 是两条独立消息；
 * 5) 轮询批次按时间正序全部入队；
 * 6) SSE 增量只维护一条临时条目原地刷新，落库 finalize 原地升级（不重启计时、
 *    不重复弹、不产生碎片气泡）；
 * 7) reset/dispose 清计时器与排队条目。
 *
 * 运行：node --test web/tests/
 */
import test from "node:test";
import assert from "node:assert/strict";

import {
  createBubbleQueue,
  applyHistoryBubbles,
  matchStreamedIndex,
  BUBBLE_MIN_SHOW_MS,
  BUBBLE_MAX_SHOW_MS,
} from "../shared/bubble-queue.js";
import { noriBubbleKey } from "../shared/chat.js";

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
  const updates = []; // {key,text,at}
  const q = createBubbleQueue({
    setTimer: clock.setTimer,
    clearTimer: clock.clearTimer,
    now: clock.now,
    onShow: (item) => shown.push({ key: item.key, text: item.text, at: clock.now() }),
    onUpdate: (item) => updates.push({ key: item.key, text: item.text, at: clock.now() }),
    onHide: (item) => hidden.push({ key: item.key, at: clock.now() }),
    ...opts,
  });
  return { q, clock, shown, hidden, updates };
}

/* ---------------- 基本顺序 / 1s / 3s ---------------- */

test("常量：最短 1s、最长 3s", () => {
  assert.equal(BUBBLE_MIN_SHOW_MS, 1000);
  assert.equal(BUBBLE_MAX_SHOW_MS, 3000);
});

test("neko #1968：正式key已见时，在播临时气泡仍原地升级最终文本", () => {
  const { q, updates, clock, hidden } = makeQueue();
  q.provisional({ token: "stream", text: "临时前缀" });
  q.seed(["id:final"]);
  clock.advance(500);
  assert.equal(q.finalize("stream", { key: "id:final", text: "最终正文" }), "finalized");
  assert.equal(updates.at(-1).text, "最终正文");
  assert.equal(updates.at(-1).key, "id:final");
  clock.advance(2500);
  assert.equal(hidden.length, 1, "升级不重启展示计时");
});

test("单条：立即展示，3s 后自动收起（空闲上限）", () => {
  const { q, clock, shown, hidden } = makeQueue();
  q.enqueue({ key: "id:1", text: "第一句" });
  assert.deepEqual(shown.map((s) => s.text), ["第一句"]);
  assert.equal(shown[0].at, 0);
  clock.advance(999);
  assert.equal(hidden.length, 0, "1s 前不收起");
  clock.advance(1);
  assert.equal(hidden.length, 0, "1s 时无排队也不收起（可停到 3s）");
  clock.advance(2000);
  assert.deepEqual(hidden.map((h) => h.key), ["id:1"], "满 3s 收起");
  assert.equal(q.currentKey(), "");
  assert.equal(clock.pending(), 0, "收起后无遗留计时器");
});

test("两条几乎同时到达：按到达顺序展示，第二条满 1s 才换", () => {
  const { q, clock, shown, hidden } = makeQueue();
  q.enqueue({ key: "id:1", text: "A" });
  q.enqueue({ key: "id:2", text: "B" });
  assert.deepEqual(shown.map((s) => s.text), ["A"], "第二条先排队");
  assert.deepEqual(q.pendingKeys(), ["id:2"]);
  clock.advance(1000);
  assert.deepEqual(shown.map((s) => s.text), ["A", "B"], "满 1s 按顺序换 B");
  assert.equal(hidden.length, 0, "换条不算收起");
  clock.advance(3000);
  assert.deepEqual(hidden.map((h) => h.key), ["id:2"], "B 无排队 → 3s 收起");
});

test("三条连发：逐条展示，每条 1s，最后一条 3s 收起（不合并、不取最新）", () => {
  const { q, clock, shown, hidden } = makeQueue();
  q.enqueue({ key: "id:1", text: "一" });
  q.enqueue({ key: "id:2", text: "二" });
  q.enqueue({ key: "id:3", text: "三" });
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
  assert.deepEqual(hidden.map((h) => h.key), ["id:3"], "最后一条 3s 收起");
});

test("展示中途到达的新消息：等满 1s 才替换（不打断正在看的这条）", () => {
  const { q, clock, shown } = makeQueue();
  q.enqueue({ key: "id:1", text: "A" });
  clock.advance(400);
  q.enqueue({ key: "id:2", text: "B" });
  assert.deepEqual(shown.map((s) => s.text), ["A"], "400ms 时 A 还在展示");
  clock.advance(599); // t=999
  assert.deepEqual(shown.map((s) => s.text), ["A"]);
  clock.advance(1); // t=1000
  assert.deepEqual(shown.map((s) => s.text), ["A", "B"], "满 1s 立即推进，不空等到 3s");
  assert.equal(shown[1].at, 1000);
});

test("空闲时新消息到达：立即展示（不排队等），旧条目被收起", () => {
  const { q, clock, shown, hidden } = makeQueue();
  q.enqueue({ key: "id:1", text: "A" });
  clock.advance(3000); // A 已收起
  assert.deepEqual(hidden.map((h) => h.key), ["id:1"]);
  q.enqueue({ key: "id:2", text: "B" });
  assert.equal(shown.at(-1).at, 3000, "空闲时立即展示");
  assert.equal(shown.at(-1).text, "B");
});

test("突发 5 条：全部依次展示，单条最长 3s、最短 1s", () => {
  const { q, clock, shown, hidden } = makeQueue();
  ["一", "二", "三", "四", "五"].forEach((text, i) => q.enqueue({ key: `id:${i + 1}`, text }));
  clock.advance(4000);
  assert.deepEqual(shown.map((s) => s.text), ["一", "二", "三", "四", "五"]);
  for (let i = 1; i < shown.length; i++) {
    assert.ok(shown[i].at - shown[i - 1].at >= BUBBLE_MIN_SHOW_MS, "相邻两条间隔 ≥ 1s");
  }
  clock.advance(1000); // t=5000, 最后一条从 4000 起算满 3s 于 7000
  clock.advance(2000);
  assert.equal(hidden.at(-1).key, "id:5");
  assert.equal(hidden.at(-1).at, 7000, "最后一条 3s 收起");
});

test("队列持续有积压时单条不会停到 3s（满 1s 就换）", () => {
  const { q, clock, shown } = makeQueue();
  q.enqueue({ key: "id:1", text: "A" });
  for (let i = 0; i < 3; i++) {
    clock.advance(1000);
    q.enqueue({ key: `id:${i + 2}`, text: `N${i}` });
  }
  const gaps = shown.slice(1).map((s, i) => s.at - shown[i].at);
  assert.deepEqual(gaps, [1000, 1000, 1000], "有排队 → 每条正好 1s");
});

/* ---------------- 去重 / 播种 / 相同内容不同 id ---------------- */

test("DB id 去重：同 key 再次入队被忽略", () => {
  const { q, clock, shown } = makeQueue();
  assert.equal(q.enqueue({ key: "id:7", text: "同一句" }), "queued");
  assert.equal(q.enqueue({ key: "id:7", text: "同一句" }), "duplicate");
  clock.advance(3000);
  assert.equal(q.enqueue({ key: "id:7", text: "同一句" }), "duplicate", "展示完也不重播");
  assert.deepEqual(shown.map((s) => s.key), ["id:7"]);
});

test("相同内容不同 id：是两条独立消息，各弹一次", () => {
  const { q, clock, shown } = makeQueue();
  assert.notEqual(noriBubbleKey({ id: 11, content: "好" }), noriBubbleKey({ id: 12, content: "好" }));
  q.enqueue({ key: "id:11", text: "好" });
  q.enqueue({ key: "id:12", text: "好" });
  clock.advance(1000);
  assert.deepEqual(shown.map((s) => s.key), ["id:11", "id:12"], "同文不同 id 不合并");
});

test("无 id 消息回落 content 键：同文本同 key 去重", () => {
  const k = noriBubbleKey({ role: "assistant", content: "你好" });
  assert.equal(k, "text:你好");
  const { q, clock, shown } = makeQueue();
  q.enqueue({ key: k, text: "你好" });
  q.enqueue({ key: k, text: "你好" });
  clock.advance(1000);
  assert.equal(shown.length, 1);
});

test("初载播种：seed 记水位且不展示；之后的同 key 不再入队", () => {
  const { q, clock, shown, hidden } = makeQueue();
  const added = q.seed(["id:1", "id:2", "id:3"]);
  assert.equal(added, 3);
  assert.equal(shown.length, 0, "历史首载绝不自动弹");
  assert.equal(clock.pending(), 0, "播种不建计时器");
  assert.equal(q.enqueue({ key: "id:3", text: "旧消息" }), "duplicate");
  assert.equal(q.enqueue({ key: "id:4", text: "新消息" }), "queued");
  assert.deepEqual(shown.map((s) => s.key), ["id:4"]);
  assert.equal(hidden.length, 0);
});

/* ---------------- SSE：临时条目 / finalize ---------------- */

test("SSE 增量：同 token 只维护一条临时气泡，原地刷新不新建", () => {
  const { q, clock, shown, updates } = makeQueue();
  q.provisional({ token: "live-1", text: "我" });
  q.provisional({ token: "live-1", text: "我觉得" });
  q.provisional({ token: "live-1", text: "我觉得这手" });
  assert.equal(shown.length, 1, "只展示一条");
  assert.deepEqual(shown[0].text, "我");
  assert.equal(updates.length, 2, "后续增量走 update 而非 show");
  assert.equal(updates.at(-1).text, "我觉得这手");
  assert.equal(q.size(), 1, "队列里没有碎片条目");
  clock.advance(1000);
  assert.equal(shown.length, 1, "1s 后也不会因为碎片条目换气泡");
});

test("SSE 增量后落库：finalize 原地升级（计时不重启、不重复弹）", () => {
  const { q, clock, shown, updates, hidden } = makeQueue();
  q.provisional({ token: "live-1", text: "半句" });
  clock.advance(400);
  q.finalize("live-1", { key: "id:9", text: "半句，落库后的全文" });
  assert.equal(shown.length, 1, "不产生第二条气泡");
  assert.equal(updates.length, 1);
  assert.equal(updates[0].key, "id:9");
  assert.equal(updates[0].text, "半句，落库后的全文");
  assert.equal(updates[0].at, 400, "升级发生在 400ms，计时不重启");
  clock.advance(600); // t=1000：仍按原起点计
  assert.equal(shown.length, 1);
  assert.equal(q.currentKey(), "id:9");
  clock.advance(2000); // t=3000
  assert.deepEqual(hidden.map((h) => h.key), ["id:9"], "按原起点 3s 收起");
  // 后续轮询重拉同一条：已见，不重播
  assert.equal(q.enqueue({ key: "id:9", text: "半句，落库后的全文" }), "duplicate");
});

test("SSE 临时气泡排队中（前一条还在展示）时落库：保留排队位置", () => {
  const { q, clock, shown } = makeQueue();
  q.enqueue({ key: "id:1", text: "A" });
  q.provisional({ token: "live-1", text: "流式中" });
  q.finalize("live-1", { key: "id:2", text: "流式落库" });
  assert.deepEqual(q.pendingKeys(), ["id:2"]);
  clock.advance(1000);
  assert.deepEqual(shown.map((s) => s.key), ["id:1", "id:2"], "满 1s 后展示落库条目");
});

test("SSE 增量与轮询批次并存：批次里含流式那条时只展示一次", () => {
  const { q, clock, shown, updates } = makeQueue();
  q.seed(["id:1"]);
  q.provisional({ token: "live-1", text: "流式" }); // 先展示（到达最早）
  // 轮询批次：id:2（另一条新消息）+ 流式这条落库为 id:3
  q.enqueue({ key: "id:2", text: "先到的另一条" });
  q.finalize("live-1", { key: "id:3", text: "流式落库" });
  assert.equal(shown.length, 1);
  assert.equal(updates.length, 1, "流式那条走 update，不新建气泡");
  assert.equal(q.currentKey(), "id:3", "当前气泡已升级为正式 id");
  assert.deepEqual(q.pendingKeys(), ["id:2"]);
  clock.advance(1000);
  assert.deepEqual(shown.map((s) => s.key), ["live:live-1", "id:2"], "满 1s 后按序展示另一条");
  assert.equal(q.currentKey(), "id:2");
  clock.advance(3000);
  assert.equal(q.size(), 0, "无碎片残留");
});

test("SSE 临时气泡排队中时落库，且批次里还有更早消息：全部按到达顺序", () => {
  const { q, clock, shown } = makeQueue();
  q.enqueue({ key: "id:1", text: "展示中" });
  q.provisional({ token: "live-1", text: "流式" }); // 排队第 1 位
  q.enqueue({ key: "id:2", text: "随批次到达" }); // 排队第 2 位
  q.finalize("live-1", { key: "id:3", text: "流式落库" });
  clock.advance(1000);
  clock.advance(1000);
  assert.deepEqual(shown.map((s) => s.key), ["id:1", "id:3", "id:2"], "临时条目按原排队位置升级");
});

test("SSE token 与批次无对应消息（落库未匹配）：临时气泡保留，后续增量仍同一条", () => {
  const { q, clock, shown, updates, hidden } = makeQueue();
  q.provisional({ token: "live-1", text: "第一段" });
  clock.advance(1000); // 无排队 → 继续停到 3s
  q.provisional({ token: "live-1", text: "第一段第二段" });
  assert.equal(updates.length, 1);
  assert.equal(shown.length, 1, "同一 token 仍是一条气泡");
  clock.advance(2000);
  assert.deepEqual(hidden.map((h) => h.key), ["live:live-1"], "按首次展示起点 3s 收起");
});

test("dropProvisional：丢弃未匹配的流式条目（不残留排队）", () => {
  const { q, clock, shown } = makeQueue();
  q.enqueue({ key: "id:1", text: "A" });
  q.provisional({ token: "live-x", text: "孤儿流式" });
  assert.equal(q.dropProvisional("live-x"), true);
  assert.deepEqual(q.pendingKeys(), []);
  clock.advance(1000);
  assert.deepEqual(shown.map((s) => s.key), ["id:1"]);
});

test("matchStreamedIndex：按落库文本定位流式那条（含媒体注解补全/空白差异）", () => {
  const items = [
    { key: "id:2", text: "另一条" },
    { key: "id:3", text: "[表情包:偷笑]" },
    { key: "id:4", text: "这手走天元，稳。" },
  ];
  assert.equal(matchStreamedIndex(items, "这手走天元"), 2, "落库文本包含流式全文");
  assert.equal(matchStreamedIndex(items, "这 手 走 天 元"), 2, "空白差异不影响匹配");
  assert.equal(matchStreamedIndex(items, "完全无关"), -1, "匹配不到返回 -1");
  assert.equal(matchStreamedIndex(items, ""), -1, "空流式文本不匹配");
  assert.equal(matchStreamedIndex(items, "[表情包:偷笑]"), 1, "媒体条也能匹配");
});

test("轮询批次里同时有其它新消息与流式落库：只升级对应那条，其余按序入队", () => {
  const { q, clock, shown, updates } = makeQueue();
  q.provisional({ token: "live-1", text: "先占中腹" });
  const fresh = [
    { key: "id:5", text: "这是之前另一条" },
    { key: "id:6", text: "先占中腹，再往两翼展开。" },
  ];
  const idx = matchStreamedIndex(fresh, "先占中腹");
  assert.equal(idx, 1);
  const [streamed] = fresh.splice(idx, 1);
  q.finalize("live-1", streamed);
  fresh.forEach((e) => q.enqueue(e));
  assert.equal(shown.length, 1, "流式那条原地升级，不新建");
  assert.equal(updates.at(-1).key, "id:6");
  assert.deepEqual(q.pendingKeys(), ["id:5"], "其它新消息正常排队");
  clock.advance(1000);
  assert.deepEqual(shown.map((s) => s.key), ["live:live-1", "id:5"]);
  assert.equal(q.currentKey(), "id:5");
});

/* ---------------- reset / dispose ---------------- */

test("reset：清计时器与排队条目、收起气泡（默认清水位）", () => {
  const { q, clock, shown, hidden } = makeQueue();
  q.seed(["id:0"]);
  q.enqueue({ key: "id:1", text: "A" });
  q.enqueue({ key: "id:2", text: "B" });
  q.provisional({ token: "live-1", text: "流式" });
  q.reset();
  assert.equal(q.size(), 0, "排队与当前条目全清");
  assert.deepEqual(hidden.map((h) => h.key), ["id:1"], "当前气泡收起");
  assert.equal(clock.pending(), 0, "计时器清空");
  clock.advance(10000);
  assert.equal(shown.length, 1, "reset 后不再有任何展示");
  assert.equal(q.enqueue({ key: "id:1", text: "A" }), "queued", "水位清空后可重新展示");
});

test("reset({keepSeen:true})：保留水位（切局防重播同 id）", () => {
  const { q } = makeQueue();
  q.seed(["id:1"]);
  q.reset({ keepSeen: true });
  assert.equal(q.enqueue({ key: "id:1", text: "A" }), "duplicate");
});

test("dispose：之后再入队/增量全部忽略，计时器清空", () => {
  const { q, clock, shown } = makeQueue();
  q.enqueue({ key: "id:1", text: "A" });
  q.dispose();
  assert.equal(q.isDisposed(), true);
  assert.equal(clock.pending(), 0);
  assert.equal(q.enqueue({ key: "id:2", text: "B" }), "disposed");
  assert.equal(q.provisional({ token: "live-1", text: "x" }), "disposed");
  clock.advance(10000);
  assert.equal(shown.length, 1);
});

test("setMaxShowMs：配置可收紧上限，但不得低于最短展示 1s", () => {
  const { q, clock, hidden } = makeQueue();
  q.setMaxShowMs(900);
  assert.equal(q.maxShowMs, 1000, "不得低于最短展示 1s");
  q.setMaxShowMs(2000);
  assert.equal(q.maxShowMs, 2000);
  q.enqueue({ key: "id:1", text: "A" });
  clock.advance(1999);
  assert.equal(hidden.length, 0, "未到配置上限不收起");
  clock.advance(1);
  assert.deepEqual(hidden.map((h) => h.key), ["id:1"], "按配置上限收起");
});

/* ---------------- 已展示临时条目不重播 / 重拉结算 ---------------- */

test("长流式（>3s）：超时收起后同一 token 的增量不再重播，只更新排队/在播条目", () => {
  const { q, clock, shown, updates, hidden } = makeQueue();
  q.provisional({ token: "live-1", text: "第一段" });
  clock.advance(3000); // 无排队 → 3s 收起
  assert.deepEqual(hidden.map((h) => h.key), ["live:live-1"]);
  assert.equal(q.isConsumed("live-1"), true, "收起即标记已消费");
  assert.equal(q.provisional({ token: "live-1", text: "第一段第二段" }), "consumed", "超时后增量不重播");
  clock.advance(10000);
  assert.equal(shown.length, 1, "整段长流式只弹一次");
  assert.equal(updates.length, 0, "收起后不再刷新（气泡已隐藏）");
});

test("长流式被下一条挤走：同 token 后续增量不重播，新消息按序展示", () => {
  const { q, clock, shown } = makeQueue();
  q.provisional({ token: "live-1", text: "流式前半" });
  q.enqueue({ key: "id:2", text: "另一条" });
  clock.advance(1000); // 流式被挤走 → id:2 展示
  assert.deepEqual(shown.map((s) => s.key), ["live:live-1", "id:2"]);
  assert.equal(q.provisional({ token: "live-1", text: "流式前半后半" }), "consumed", "被挤走的流式不再重播");
  clock.advance(3000);
  assert.equal(shown.length, 2, "没有第三条气泡");
});

test("在播/排队中的临时条目：后续增量仍原地刷新", () => {
  const { q, clock, shown, updates } = makeQueue();
  q.provisional({ token: "live-1", text: "A" });
  clock.advance(500);
  assert.equal(q.provisional({ token: "live-1", text: "AB" }), "updated");
  assert.equal(updates.at(-1).text, "AB");
  q.enqueue({ key: "id:2", text: "另一条" });
  q.provisional({ token: "live-1", text: "ABC" }); // 在播 → 仍更新
  assert.equal(updates.at(-1).text, "ABC");
  clock.advance(500);
  assert.deepEqual(shown.map((s) => s.key), ["live:live-1", "id:2"], "仍按序推进");
});

test("已展示过的临时条目 finalize：只记正式 key 为已见，绝不重播", () => {
  const { q, clock, shown, updates, hidden } = makeQueue();
  q.provisional({ token: "live-1", text: "半句" });
  clock.advance(3000); // 超时收起
  assert.deepEqual(hidden.map((h) => h.key), ["live:live-1"]);
  const r = q.finalize("live-1", { key: "id:9", text: "半句，落库全文" });
  assert.equal(r, "already-shown", "不重新入队");
  assert.equal(q.hasSeen("id:9"), true, "正式 key 记为已见（后续重拉不重播）");
  assert.equal(shown.length, 1, "不再弹第二条");
  assert.equal(updates.length, 0);
  assert.equal(q.enqueue({ key: "id:9", text: "半句，落库全文" }), "duplicate");
});

test("被挤走后 finalize：同样只记水位，不重播", () => {
  const { q, clock, shown } = makeQueue();
  q.provisional({ token: "live-1", text: "流式" });
  q.enqueue({ key: "id:2", text: "另一条" });
  clock.advance(1000);
  assert.equal(q.finalize("live-1", { key: "id:3", text: "流式" }), "already-shown");
  assert.equal(q.hasSeen("id:3"), true);
  clock.advance(3000);
  assert.deepEqual(shown.map((s) => s.key), ["live:live-1", "id:2"]);
});

test("在播临时条目 finalize 后同 key 再入队：不重复", () => {
  const { q, clock, shown } = makeQueue();
  q.provisional({ token: "live-1", text: "半句" });
  clock.advance(400);
  assert.equal(q.finalize("live-1", { key: "id:9", text: "半句全文" }), "finalized");
  assert.equal(q.isConsumed("live-1"), true);
  assert.equal(q.provisional({ token: "live-1", text: "半句全文" }), "consumed", "结算后增量不重播");
  assert.equal(q.finalize("live-1", { key: "id:9", text: "半句全文" }), "already-shown", "重复结算不重播");
  clock.advance(2600); // t=3000，按原起点收起
  assert.deepEqual(shown.map((s) => s.key), ["live:live-1"]);
});

/* ---------------- applyHistoryBubbles：重拉接线 ---------------- */

const histKey = (m) => (m.id != null ? `id:${m.id}` : "");
const histPayload = (m) => ({ text: m.content, media: [] });

function reloadWith(q, items, live) {
  return applyHistoryBubbles(q, items, {
    keyOf: histKey,
    payloadOf: histPayload,
    ...live,
  });
}

test("重拉未匹配：保留 token 与前缀（不清空），下一次重拉命中后结算", () => {
  const { q, clock, shown, updates } = makeQueue();
  q.seed(["id:1"]);
  q.provisional({ token: "live-1", text: "我觉" });
  clock.advance(400);
  q.provisional({ token: "live-1", text: "我觉得这手" });
  // 第一次重拉：历史还没带上这条回复 → 未匹配
  const s1 = reloadWith(q, [{ id: 1, role: "assistant", content: "开场" }], {
    liveToken: "live-1",
    liveText: "我觉得这手",
  });
  assert.equal(s1.settled, false);
  assert.equal(s1.token, "live-1", "token 保留");
  assert.equal(s1.text, "我觉得这手", "前缀保留（下次重拉还能匹配）");
  assert.equal(q.currentKey(), "live:live-1", "在播临时条目仍在");
  // 后续增量继续同一条（前缀没丢）
  assert.equal(q.provisional({ token: "live-1", text: "我觉得这手很好" }), "updated");
  // 第二次重拉：历史落库 → 命中并原地升级
  const s2 = reloadWith(
    q,
    [
      { id: 1, role: "assistant", content: "开场" },
      { id: 2, role: "assistant", content: "我觉得这手很好，走这里。" },
    ],
    { liveToken: "live-1", liveText: "我觉得这手很好" }
  );
  assert.equal(s2.settled, true);
  assert.equal(s2.token, "", "结算后清空 token");
  assert.equal(s2.text, "", "结算后清空前缀");
  assert.equal(updates.at(-1).key, "id:2", "原地升级为正式 key");
  assert.equal(shown.length, 1, "全程只有一条气泡");
  clock.advance(2600);
  assert.equal(q.currentKey(), "");
});

test("流式匹配拒绝反向包含与单字前缀", () => {
  assert.equal(matchStreamedIndex([{ text: "旧句" }], "旧句后面还有新正文"), -1);
  assert.equal(matchStreamedIndex([{ text: "我下这里" }], "我"), -1);
});

test("相同短语不与已播种旧历史结算", () => {
  const { q, updates } = makeQueue();
  q.seed(["id:1"]);
  q.provisional({ token: "new-turn", text: "走这里" });
  const result = reloadWith(q, [{ id: 1, role: "assistant", content: "走这里" }], {
    liveToken: "new-turn", liveText: "走这里",
  });
  assert.equal(result.settled, false);
  assert.equal(result.token, "new-turn");
  assert.equal(q.currentKey(), "live:new-turn");
  assert.equal(updates.length, 0);
});

test("重拉批次含其它新消息：先按序入队，流式那条原地结算不重复", () => {
  const { q, clock, shown, updates } = makeQueue();
  q.seed(["id:1"]);
  q.provisional({ token: "live-1", text: "先占中腹" });
  const items = [
    { id: 1, role: "assistant", content: "开场" },
    { id: 5, role: "assistant", content: "这是之前另一条" },
    { id: 6, role: "assistant", content: "先占中腹，再往两翼展开。" },
  ];
  const s = reloadWith(q, items, { liveToken: "live-1", liveText: "先占中腹" });
  assert.equal(s.settled, true);
  assert.equal(s.fresh, 2, "两条新消息");
  assert.equal(updates.at(-1).key, "id:6");
  assert.deepEqual(q.pendingKeys(), ["id:5"]);
  clock.advance(1000);
  assert.deepEqual(shown.map((s2) => s2.key), ["live:live-1", "id:5"]);
});

test("初载 + SSE 抢跑：历史播种后仍结算流式条目（首载竞态）", () => {
  const { q, clock, shown } = makeQueue();
  q.provisional({ token: "live-1", text: "开场白" });
  const s = reloadWith(
    q,
    [
      { id: 1, role: "user", content: "来一局" },
      { id: 2, role: "assistant", content: "开场白，落库了。" },
    ],
    { firstLoad: true, liveToken: "live-1", liveText: "开场白" }
  );
  assert.equal(s.first, true);
  assert.equal(s.settled, true, "初载批次也能命中流式条目");
  assert.equal(q.hasSeen("id:2"), true, "历史被播种");
  assert.equal(shown.length, 1, "只展示流式那一条（历史旧消息不弹）");
  clock.advance(3000);
  assert.equal(q.currentKey(), "");
});

test("初载未匹配：保留流式状态，后续增量与下次重拉继续（不丢前缀）", () => {
  const { q, updates } = makeQueue();
  const s1 = reloadWith(q, [{ id: 1, role: "assistant", content: "旧消息" }], {
    firstLoad: true,
    liveToken: "live-1",
    liveText: "半句",
  });
  assert.equal(s1.settled, false);
  assert.equal(s1.token, "live-1");
  assert.equal(s1.text, "半句");
  // 初载重拉前临时条目尚未创建：首次增量正常入队，后续增量原地刷新
  assert.equal(q.provisional({ token: "live-1", text: "半句续" }), "queued");
  assert.equal(q.provisional({ token: "live-1", text: "半句续完了" }), "updated");
  const s2 = reloadWith(q, [{ id: 2, role: "assistant", content: "半句续完了" }], {
    liveToken: "live-1",
    liveText: "半句续完了",
  });
  assert.equal(s2.settled, true);
  assert.equal(updates.at(-1).key, "id:2");
});

test("已消费 token 的 finalize 经接线：不重播（只记水位）", () => {
  const { q, clock, shown } = makeQueue();
  q.seed(["id:1"]);
  q.provisional({ token: "live-1", text: "流式" });
  clock.advance(3000); // 超时收起
  const s = reloadWith(
    q,
    [
      { id: 1, role: "assistant", content: "开场" },
      { id: 2, role: "assistant", content: "流式，落库" },
    ],
    { liveToken: "live-1", liveText: "流式" }
  );
  assert.equal(s.settled, true);
  assert.equal(s.fresh, 1);
  assert.equal(q.hasSeen("id:2"), true, "正式 key 记为已见");
  clock.advance(5000);
  assert.equal(shown.length, 1, "超时收起的流式消息不会因落库再弹一次");
});

test("reset/dispose 清空已消费 token 记录（切会话不串台）", () => {
  const { q } = makeQueue();
  q.provisional({ token: "live-1", text: "A" });
  q.reset();
  assert.equal(q.isConsumed("live-1"), false, "reset 后同一 token 可重新展示");
  assert.equal(q.provisional({ token: "live-1", text: "A" }), "queued");
});

/** 与 match.js reloadChatHistory 相同的选择逻辑：所有未见过诺里消息按正序入队。 */
function collectFresh(items, queue) {
  const fresh = [];
  for (const m of items) {
    if (!m || m.role !== "assistant") continue;
    const key = noriBubbleKey(m);
    if (!key || queue.hasSeen(key)) continue;
    fresh.push({ key, text: m.content });
  }
  return fresh;
}

test("轮询批次里两条新回复（含表情包）：按时间正序逐条展示，不丢表情包", () => {
  const { q, clock, shown } = makeQueue();
  const initial = [{ id: 1, role: "assistant", content: "开场" }];
  q.seed(initial.map(noriBubbleKey)); // 初载播种
  // 批次：id2 文本 + id3 表情包（旧逻辑 newestAssistant 只取 id3，id2 丢失）
  const batch = [
    ...initial,
    { id: 2, role: "assistant", content: "先说一句" },
    { id: 3, role: "assistant", content: "[表情包:偷笑]" },
  ];
  collectFresh(batch, q).forEach((e) => q.enqueue(e));
  clock.advance(1000);
  assert.deepEqual(shown.map((s) => s.text), ["先说一句", "[表情包:偷笑]"], "两条都展示且顺序正确");
});

test("初载批次（多轮历史）：只播种，不展示任何一条", () => {
  const { q, clock, shown } = makeQueue();
  const history = [
    { id: 1, role: "user", content: "来一局" },
    { id: 2, role: "assistant", content: "好呀" },
    { id: 3, role: "user", content: "嗯" },
    { id: 4, role: "assistant", content: "[表情包:偷笑]" },
  ];
  q.seed(history.map(noriBubbleKey).filter(Boolean));
  clock.advance(5000);
  assert.equal(shown.length, 0, "历史首载绝不自动播放");
  assert.equal(q.enqueue({ key: "id:4", text: "[表情包:偷笑]" }), "duplicate");
});
