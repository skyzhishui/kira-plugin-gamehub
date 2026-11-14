/* bubble-queue.js — 座位发言气泡的排队播放器（纯逻辑，Node 可测）。
 *
 * 需求（宿主主 agent 座位与陪练对手座位共用同一套规则）：
 * - 多条消息按「到达顺序」逐条展示：不合并成一条、不取最新一条；
 * - 每条至少展示 BUBBLE_MIN_SHOW_MS（1s）：不足 1s 时新消息只排队，
 *   到点才换，保证肉眼能看清每一条；
 * - 有排队时第 1s 就换下一条（不空等）；
 * - 无排队时最多展示 BUBBLE_MAX_SHOW_MS（3s）后自动收起（空闲自动隐藏）；
 * - 历史首载只播种（seed）不播放；按 key（DB id）去重；
 *   内容相同但 id 不同 → 视为两条独立消息，各自展示；
 * - SSE 增量用「临时条目」原地更新（绝不逐 token 新建气泡）；落库后用
 *   finalize 把同一条临时条目升级为正式条目，计时不重启（不闪断、不重复）。
 *
 * 计时器/时钟全部可注入：测试用假时钟即可确定性地验证 1s/3s 与顺序。
 * 本模块不碰 DOM：onShow / onUpdate / onHide 由调用方接到真实气泡上。
 */

export const BUBBLE_MIN_SHOW_MS = 1000;
export const BUBBLE_MAX_SHOW_MS = 3000;

function positiveMs(value, fallback) {
  const n = Math.round(Number(value));
  return Number.isFinite(n) && n > 0 ? n : fallback;
}

/** 在重拉批次里找「流式那条」落库后的消息：从后往前比对文本
 * （落库文本通常含流式全文；只接受落库文本包含至少两个字符的流式前缀）。
 * 返回下标，-1 = 本批没有对应消息（流式条目保留，等下次重拉结算）。 */
export function matchStreamedIndex(items, buffer) {
  const target = String(buffer == null ? "" : buffer).replace(/\s+/g, "");
  if (Array.from(target).length < 2) return -1;
  const arr = Array.isArray(items) ? items : [];
  for (let i = arr.length - 1; i >= 0; i--) {
    const text = String((arr[i] && arr[i].text) || "").replace(/\s+/g, "");
    if (!text) continue;
    if (text.includes(target)) return i;
  }
  return -1;
}

/** 历史重拉 → 气泡队列接线（match.js reloadChatHistory 的核心，纯逻辑可测）。
 *
 * items 为宿主历史（时间正序）；keyOf(msg) 给气泡键，payloadOf(msg) 给
 * {text, media}。行为：
 * - firstLoad：把历史里所有宿主主 agent key 播种为已见（不播放）；
 * - 其余重拉：所有未见过的宿主主 agent 消息按时间正序入队；
 * - 流式临时条目结算：在未见消息里按 liveText 定位落库消息并 finalize；
 *   初载在全部条目匹配，覆盖 SSE 抢跑历史返回的竞态；
 * - 未匹配时**保留** liveToken/liveText 交给下次重拉继续匹配（前缀不丢）。
 * 返回更新后的流式状态 {token, text}；settled=true 表示已完成结算。
 */
export function applyHistoryBubbles(queue, items, {
  firstLoad = false,
  liveToken = "",
  liveText = "",
  keyOf = () => "",
  payloadOf = () => ({ text: "", media: [] }),
} = {}) {
  const entries = [];
  for (const m of Array.isArray(items) ? items : []) {
    if (!m || m.role !== "assistant") continue;
    const key = String(keyOf(m) == null ? "" : keyOf(m));
    if (!key) continue;
    const payload = payloadOf(m) || {};
    entries.push({ key, item: { key, text: payload.text, media: payload.media } });
  }
  if (firstLoad) queue.seed(entries.map((e) => e.key));
  const fresh = firstLoad ? [] : entries.filter((e) => !queue.hasSeen(e.key));
  let settled = false;
  if (liveToken) {
    // 正常重拉只匹配未见消息，避免相同短语命中旧历史；初载仍覆盖 SSE 抢跑。
    const candidates = firstLoad ? entries : fresh;
    const idx = matchStreamedIndex(candidates.map((e) => e.item), liveText);
    if (idx >= 0) {
      queue.finalize(liveToken, candidates[idx].item);
      settled = true;
    }
  }
  if (!firstLoad) fresh.forEach((e) => queue.enqueue(e.item));
  const keep = !!liveToken && !settled;
  return {
    first: !!firstLoad,
    fresh: fresh.length,
    settled,
    token: keep ? String(liveToken) : "",
    text: keep ? String(liveText == null ? "" : liveText) : "",
  };
}

/** 生成排队器。
 * 回调：
 * - onShow(item)：开始展示一条（首帧/换下一条）；
 * - onUpdate(item)：同一条内容刷新（SSE 增量 / 临时条目升级），不重置计时；
 * - onHide(item)：收起当前气泡。
 * item 形如 {key, text, media, provisional?, token?}。 */
export function createBubbleQueue({
  setTimer = (fn, ms) => setTimeout(fn, ms),
  clearTimer = (id) => clearTimeout(id),
  now = () => Date.now(),
  minShowMs = BUBBLE_MIN_SHOW_MS,
  maxShowMs = BUBBLE_MAX_SHOW_MS,
  onShow = () => {},
  onUpdate = () => {},
  onHide = () => {},
} = {}) {
  let minMs = positiveMs(minShowMs, BUBBLE_MIN_SHOW_MS);
  let maxMs = Math.max(minMs, positiveMs(maxShowMs, BUBBLE_MAX_SHOW_MS));
  const seen = new Set();
  const consumed = new Set(); // 已离开屏幕/已结算的流式 token：后续增量与 finalize 不再重播
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
    // 被下一条挤走的临时条目也算「展示过」：同一 token 的后续增量不重播
    if (current && current.provisional) consumed.add(current.token);
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
    if (item.provisional) consumed.add(item.token); // 超时收起：同一 token 不再重播
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
      media: Array.isArray(item.media) ? item.media.slice(0, 2) : [],
      provisional: false,
    };
  }

  function enqueue(raw) {
    if (disposed) return "disposed";
    if (raw && raw.provisional) return upsertProvisional(raw);
    const item = normalize(raw);
    if (!item) return "ignored";
    if (seen.has(item.key)) return "duplicate";
    seen.add(item.key);
    queue.push(item);
    pump();
    return "queued";
  }

  /** SSE 增量：同 token 只维护一条临时条目，内容原地刷新。
   * 临时条目不进 seen（落库后 finalize 换成正式 id 键）。
   * token 一旦已离开屏幕（超时/被挤走/已结算）就不再重播：后续增量返回
   * "consumed"（调用方可忽略），避免长流式回复每 3s 重复弹同一条。 */
  function upsertProvisional(raw) {
    if (disposed) return "disposed";
    const token = String((raw && (raw.token || raw.key)) || "");
    if (!token) return "ignored";
    if (consumed.has(token)) return "consumed";
    const next = {
      key: `live:${token}`,
      token,
      text: String(raw.text == null ? "" : raw.text),
      media: Array.isArray(raw.media) ? raw.media.slice(0, 2) : [],
      provisional: true,
    };
    if (current && current.provisional && current.token === token) {
      current = { ...current, ...next };
      onUpdate(current);
      return "updated";
    }
    const qi = queue.findIndex((q) => q.provisional && q.token === token);
    if (qi >= 0) {
      queue[qi] = { ...queue[qi], ...next };
      return "updated";
    }
    queue.push(next);
    pump();
    return "queued";
  }

  /** 落库后把临时条目换成正式条目：正在展示的原地升级（计时继续），
   * 排队中的保留原位置。临时条目已展示过（超时/被挤走）时只记正式 key
   * 为已见、绝不重新弹；没有对应临时条目时按普通新消息入队。 */
  function finalize(token, raw) {
    if (disposed) return "disposed";
    const t = String(token == null ? "" : token);
    if (!t) return "ignored";
    const item = normalize(raw);
    const isCurrent = !!(current && current.provisional && current.token === t);
    const qi = isCurrent ? -1 : queue.findIndex((q) => q.provisional && q.token === t);
    const hadProvisional = isCurrent || qi >= 0;

    if (hadProvisional) {
      queue = queue.filter((q) => !(q.provisional && q.token === t));
      if (!item) {
        consumed.add(t);
        if (isCurrent) hide();
        return "dropped";
      }
      if (seen.has(item.key) && !isCurrent) {
        consumed.add(t);
        return "duplicate";
      }
      seen.add(item.key);
      consumed.add(t);
      if (isCurrent) {
        // 同一条消息：只换内容/正式键，保留 shownAt 与 tick —— 不重启 1s/3s
        current = item;
        onUpdate(current);
        return "finalized";
      }
      queue.splice(Math.min(qi, queue.length), 0, item);
      pump();
      return "finalized";
    }
    if (consumed.has(t)) {
      // 临时条目已展示完/已结算：只记正式 key 为已见，不重播
      if (item && !seen.has(item.key)) seen.add(item.key);
      return item ? "already-shown" : "consumed";
    }
    return enqueue(item || { key: "" });
  }

  /** 丢弃某条临时条目（对流式内容已落库但未匹配到消息时的兜底）。
   * 丢弃后同 token 视为已消费：后续增量不再重播。 */
  function dropProvisional(token) {
    const t = String(token == null ? "" : token);
    if (!t) return false;
    let dropped = false;
    if (current && current.provisional && current.token === t) {
      dropped = true;
      hide();
    }
    const before = queue.length;
    queue = queue.filter((q) => !(q.provisional && q.token === t));
    if (queue.length !== before) dropped = true;
    if (dropped) consumed.add(t);
    return dropped;
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
    consumed.clear();
  }

  function dispose() {
    reset();
    disposed = true;
  }

  return {
    enqueue,
    provisional: upsertProvisional,
    finalize,
    dropProvisional,
    seed,
    reset,
    dispose,
    hasSeen: (key) => seen.has(String(key == null ? "" : key)),
    isConsumed: (token) => consumed.has(String(token == null ? "" : token)),
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
    currentItem: () => (current ? { ...current } : null),
    isDisposed: () => disposed,
  };
}
