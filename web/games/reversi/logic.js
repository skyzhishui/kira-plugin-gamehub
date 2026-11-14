/* games/reversi/logic.js — 黑白棋纯逻辑（零 DOM，Node 可直测）。
 *
 * 边界：本文件**不实现黑白棋规则**。翻子/合法点/终局/胜负的唯一来源是
 * 服务端 view（engine/games/reversi）：前端只做坐标命名、计数、文案与命中
 * 判断，避免 Python/JS 两套翻子逻辑漂移（见 docs/dev/web-mode-contract.md §1.2）。
 *
 * 视图契约：
 *   view = {
 *     size: 8, cells: [64 项 0 空 / 1 黑 / 2 白], toPlay: 1 | 2,
 *     moves: 落子数（不含 pass）, last?: 索引, flipped?: 索引[],
 *     passed?: 1 | 2, over?: true,
 *     legal: { "索引": 翻子数 },  // 服务端唯一来源
 *     you: 1 | 2 | null,          // 观战为 null
 *   }
 * 索引 = y * size + x，y=0 为最上一排；列 A-H 左→右，行号自下往上 1 起。
 */

/** 与后端 engine/coords.py 的 COLUMNS 一致：跳过 I。8×8 只用到 A-H。 */
export const COLUMNS = "ABCDEFGHJKLMNOPQRSTUVWXYZ";
export const SIZE = 8;
export const STONE_NAMES = { 1: "黑", 2: "白" };
export const STONE_WORDS = { 1: "黑方", 2: "白方" };

export function normalizeSize(value) {
  const n = Math.floor(Number(value));
  if (!Number.isFinite(n) || n < 2 || n > COLUMNS.length) return SIZE;
  return n;
}

/** 列字母（0 → A；8×8 只到 H）。 */
export function columnName(x) {
  return COLUMNS[x] || "?";
}

/** 索引点 → 坐标名（如 (3,2) → "D6"，行号从底部 1 起）。 */
export function coordName(x, y, size = SIZE) {
  const n = normalizeSize(size);
  return `${columnName(x)}${n - y}`;
}

/** 坐标名 → 点；非法/越界返回 null。 */
export function parseCoord(text, size = SIZE) {
  const n = normalizeSize(size);
  const m = /^([A-Za-z])(\d{1,2})$/.exec(String(text == null ? "" : text).trim());
  if (!m) return null;
  const x = COLUMNS.indexOf(m[1].toUpperCase());
  const y = n - parseInt(m[2], 10);
  if (x < 0 || x >= n || y < 0 || y >= n) return null;
  return { x, y };
}

export function cellIndex(x, y, size = SIZE) {
  const n = normalizeSize(size);
  if (!Number.isInteger(x) || !Number.isInteger(y)) return -1;
  if (x < 0 || y < 0 || x >= n || y >= n) return -1;
  return y * n + x;
}

export function pointOf(index, size = SIZE) {
  const n = normalizeSize(size);
  const i = Math.trunc(Number(index));
  if (!Number.isFinite(i) || i < 0 || i >= n * n) return null;
  return { x: i % n, y: Math.floor(i / n) };
}

/** 子数（cells 是服务端真相；moves 只表示手数，两者不互相推导）。 */
export function stoneCounts(cells) {
  let black = 0;
  let white = 0;
  if (Array.isArray(cells)) {
    for (const v of cells) {
      if (v === 1) black += 1;
      else if (v === 2) white += 1;
    }
  }
  const total = Array.isArray(cells) ? cells.length : 0;
  return { black, white, empty: Math.max(0, total - black - white) };
}

/** view.legal → Map<索引, 翻子数>。容忍字符串键、脏值；绝不重算规则。 */
export function legalMap(view) {
  const out = new Map();
  const src = view && view.legal;
  if (!src || typeof src !== "object" || Array.isArray(src)) return out;
  const limit = normalizeSize(view.size) ** 2;
  for (const [key, value] of Object.entries(src)) {
    const index = Number(key);
    const flips = Number(value);
    if (!Number.isInteger(index) || index < 0 || index >= limit) continue;
    if (!Number.isFinite(flips) || flips < 0) continue;
    out.set(index, Math.trunc(flips));
  }
  return out;
}

export function legalIndices(view) {
  return [...legalMap(view).keys()].sort((a, b) => a - b);
}

/** 我的执色：view.you（1|2|null）优先；旧视图缺字段时回退座位号 +1；观战 0。
 * 契约里 you=null 表示观战——显式给出即定案，绝不被座位号翻案。 */
export function youOf(view, match) {
  if (view && Object.prototype.hasOwnProperty.call(view, "you") && view.you !== undefined) {
    const you = Number(view.you);
    if (you === 1 || you === 2) return you;
    return 0; // 显式 null（观战）/非法值 → 观战
  }
  const me = match && typeof match.me === "number" ? match.me : -1;
  return me === 0 || me === 1 ? me + 1 : 0;
}

export function lastMoveIndex(view) {
  if (!view) return -1;
  const n = Number(view.last);
  return Number.isInteger(n) && n >= 0 ? n : -1;
}

/** pass 事实文案（只读 view.passed；pass 不进 log，所以不从记录推断）。 */
export function passHintText(view, you) {
  const passed = Number(view && view.passed);
  if (passed !== 1 && passed !== 2) return "";
  if (!you) return `${STONE_WORDS[passed]}无子可落，已跳过一手`;
  if (passed === you) return "你无子可落，已自动跳过一手";
  return "对方无子可落，已跳过一手";
}

/** 最后一手说明（坐标 + 本手翻子数）。 */
export function lastMoveText(view) {
  const index = lastMoveIndex(view);
  if (index < 0) return "";
  const size = normalizeSize(view && view.size);
  const point = pointOf(index, size);
  if (!point) return "";
  const name = coordName(point.x, point.y, size);
  const flipped = Array.isArray(view && view.flipped) ? view.flipped.length : 0;
  return flipped > 0 ? `最后一步 ${name} · 翻 ${flipped} 子` : `最后一步 ${name}`;
}

/**
 * 计数条文案（DOM 由 games/reversi/index.js 渲染，纯文本在此可测）。
 * 第二行优先 pass 事实，其次最后一手；两者都没有时留空（高度固定不跳动）。
 */
export function metaText(view, you) {
  const { black, white } = stoneCounts(view && view.cells);
  const moves = Math.max(0, Math.trunc(Number(view && view.moves) || 0));
  // 终局优先展示最后一手（胜利/吃光收尾），不把「跳过一手」留在结果页
  const note = view && view.over
    ? lastMoveText(view)
    : passHintText(view, you) || lastMoveText(view);
  return {
    black,
    white,
    youText: you === 1 ? "你执黑" : you === 2 ? "你执白" : "观战",
    movesText: moves > 0 ? `第 ${moves} 手` : "尚未落子",
    note,
  };
}

/** 该点现在能否落子：合法点由服务端给出，再叠加本方回合/忙碌/终局开关。 */
export function playableAt(view, index, { you, canAct, busy, over } = {}) {
  if (!view || over || view.over) return false;
  if (!(you === 1 || you === 2)) return false; // 观战/未知视角不接交互
  if (Number(view.toPlay) !== you) return false;
  if (!canAct || busy) return false;
  return legalMap(view).has(Number(index));
}

/** 棋盘整体是否可落子（决定是否绘制呼吸的合法点与光标）。 */
export function isPlayable(view, opts = {}) {
  if (!view || opts.over || view.over) return false;
  if (!(opts.you === 1 || opts.you === 2)) return false;
  if (Number(view.toPlay) !== opts.you) return false;
  if (!opts.canAct || opts.busy) return false;
  return legalMap(view).size > 0;
}

/** 是否新的一手（moves 增长）：聊天/代下等同局面帧重复送达时不重播动效。 */
export function isFreshMove(prev, next) {
  if (!prev || !next) return false; // 首帧静态呈现，不做入场动画
  const p = Math.trunc(Number(prev.moves) || 0);
  const n = Math.trunc(Number(next.moves) || 0);
  if (n <= p) return false;
  if (lastMoveIndex(next) < 0) return false;
  return true;
}

/** 读屏标签：盘面事实（尺寸/双方子数/轮到谁/可落点数）。 */
export function boardAriaLabel(view, you) {
  const size = normalizeSize(view && view.size);
  const { black, white } = stoneCounts(view && view.cells);
  const over = !!(view && view.over);
  const legal = legalMap(view).size;
  const side = you === 1 ? "你执黑" : you === 2 ? "你执白" : "观战";
  const turn = over ? "对局结束" : `轮到${STONE_NAMES[Number(view && view.toPlay)] || "?"}方`;
  return `黑白棋棋盘，${size}×${size}，${side}，${turn}，可落点 ${legal} 个，黑 ${black} 子、白 ${white} 子。方向键移动光标，回车落子。`;
}