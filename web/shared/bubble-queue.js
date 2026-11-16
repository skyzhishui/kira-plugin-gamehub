/* bubble-queue.js — seat-speech bubble queue (pure logic, testable in Node).
 *
 * Requirements (the host-agent seat and practice/advanced-opponent seats share one
 * rule set):
 * - Messages are shown one at a time, in arrival order: never merged into one, never
 *   collapsed to "the latest";
 * - Each entry stays at least BUBBLE_MIN_SHOW_MS (1s): a message arriving earlier
 *   than that only queues, so the eye can read every one;
 * - With a backlog the next entry rotates in right at 1s (no idle waiting);
 * - Without a backlog an entry auto-hides after at most BUBBLE_MAX_SHOW_MS (3s);
 * - The first snapshot only seeds the watermark, it never plays; dedupe is by key.
 *   Same content under different keys = two independent messages, each shown once.
 *
 * The match page feeds this queue from match.chat snapshot deltas (there is no SSE or
 * streaming reply), so it only plays already-persisted entries in order. The old
 * streaming provisional entry / finalize / history-refill wiring had no callers and
 * was removed.
 *
 * Timers and clock are fully injectable: a fake clock in tests verifies the 1s/3s
 * bounds and the order deterministically. This module never touches the DOM: the
 * caller wires onShow / onHide to the real bubble element.
 */

export const BUBBLE_MIN_SHOW_MS = 1000;
export const BUBBLE_MAX_SHOW_MS = 3000;

function positiveMs(value, fallback) {
  const n = Math.round(Number(value));
  return Number.isFinite(n) && n > 0 ? n : fallback;
}

/** 生成排队器。
 * 回调：
 * - onShow(item)：开始展示一条（首帧/换下一条）；
 * - onHide(item)：收起当前气泡。
 * Items look like {key, text}. */
export function createBubbleQueue({
  setTimer = (fn, ms) => setTimeout(fn, ms),
  clearTimer = (id) => clearTimeout(id),
  now = () => Date.now(),
  minShowMs = BUBBLE_MIN_SHOW_MS,
  maxShowMs = BUBBLE_MAX_SHOW_MS,
  onShow = () => {},
  onHide = () => {},
} = {}) {
  let minMs = positiveMs(minShowMs, BUBBLE_MIN_SHOW_MS);
  let maxMs = Math.max(minMs, positiveMs(maxShowMs, BUBBLE_MAX_SHOW_MS));
  const seen = new Set();
  let queue = [];
  let current = null;
  let shownAt = 0;
  let tick = 0;
  let disposed = false;

  function elapsed() {
    return Math.max(0, now() - shownAt);
  }

  function clearTick() {
    if (tick) {
      clearTimer(tick);
      tick = 0;
    }
  }

  function schedule(ms) {
    clearTick();
    tick = setTimer(() => {
      tick = 0;
      onTick();
    }, Math.max(0, Math.round(ms)));
  }

  function start(item) {
    current = item;
    shownAt = now();
    onShow(item);
    schedule(minMs); // 最早 1s 后才可能换下一条 / 收起
  }

  function showNext() {
    const next = queue.shift();
    if (!next) {
      hide();
      return;
    }
    start(next);
  }

  function hide() {
    if (!current) {
      // 防御：没有当前条目但队列还有，直接推进
      if (queue.length) showNext();
      return;
    }
    const item = current;
    current = null;
    clearTick();
    onHide(item);
    if (queue.length) showNext();
  }

  function onTick() {
    if (disposed || !current) {
      if (!disposed && !current && queue.length) showNext();
      return;
    }
    if (queue.length) {
      showNext(); // 有排队：满 1s 立即按顺序换下一条
      return;
    }
    const remain = maxMs - elapsed();
    if (remain <= 0) hide(); // 无排队：展示到 3s 上限后自动收起
    else schedule(remain);
  }

  /** 当前条目空闲/到点时若有排队就推进；新消息在 1s 前只排队。 */
  function pump() {
    if (!current) {
      if (queue.length) showNext();
      return;
    }
    if (queue.length && elapsed() >= minMs) showNext();
  }

  function normalize(item) {
    if (!item || typeof item !== "object") return null;
    const key = String(item.key == null ? "" : item.key);
    if (!key) return null;
    return {
      key,
      text: String(item.text == null ? "" : item.text),
    };
  }

  function enqueue(raw) {
    if (disposed) return "disposed";
    const item = normalize(raw);
    if (!item) return "ignored";
    if (seen.has(item.key)) return "duplicate";
    seen.add(item.key);
    queue.push(item);
    pump();
    return "queued";
  }

  /** 初载历史播种：记下已见 key，不展示（历史旧消息绝不自动弹）。 */
  function seed(keys) {
    let added = 0;
    for (const k of Array.isArray(keys) ? keys : []) {
      const s = String(k == null ? "" : k);
      if (!s || seen.has(s)) continue;
      seen.add(s);
      added += 1;
    }
    return added;
  }

  /** 清空展示与排队（切会话/重绑/销毁用）。keepSeen 保留已见水位。 */
  function reset({ keepSeen = false } = {}) {
    clearTick();
    queue = [];
    if (current) {
      const item = current;
      current = null;
      onHide(item);
    }
    if (!keepSeen) seen.clear();
  }

  function dispose() {
    reset();
    disposed = true;
  }

  return {
    enqueue,
    seed,
    reset,
    dispose,
    hasSeen: (key) => seen.has(String(key == null ? "" : key)),
    setMaxShowMs: (ms) => {
      maxMs = Math.max(minMs, positiveMs(ms, maxMs));
    },
    get maxShowMs() {
      return maxMs;
    },
    get minShowMs() {
      return minMs;
    },
    size: () => queue.length + (current ? 1 : 0),
    pendingKeys: () => queue.map((q) => q.key),
    currentKey: () => (current ? current.key : ""),
    isDisposed: () => disposed,
  };
}