/* games/doudizhu/logic.js — 斗地主页面纯逻辑（Node 可直测，零浏览器依赖）。
 *
 * 原则：前端零规则。牌型是否成立、能不能压、叫分是否合法全部由服务端裁决；
 * 本文件只做「显示」与「把点选变成服务端认识的 move 字符串」：
 * - 牌 ID 与服务端一致：S3 / H10 / DA / C2 / BJ / RJ（花色 + 点数）。
 * - 提交 move：bid 用 "1"/"2"/"3"，不叫/不要用 "pass"，出牌用空格分隔的
 *   点数文本（"3 3 3 4"、"10 J Q K A"、王用 "BJ"/"RJ"）。服务端按手牌
 *   自动分配花色，前端绝不自行校验牌型或压牌。
 * - 唯一提示来源：view.hints（[{cards, combo, move, name}]）。没有 hints 就
 *   不提示，不发明推荐。
 */

/* ---------------- 牌面 ---------------- */

export const SUIT_SYMBOLS = { S: "♠", H: "♥", C: "♣", D: "♦" };
export const SUIT_NAMES = { S: "黑桃", H: "红桃", C: "梅花", D: "方块" };
const SUIT_ORDER = { S: 0, H: 1, C: 2, D: 3 };
const RANK_ORDER = { 3: 3, 4: 4, 5: 5, 6: 6, 7: 7, 8: 8, 9: 9, 10: 10, J: 11, Q: 12, K: 13, A: 14, 2: 15, BJ: 16, RJ: 17 };
const RANK_TEXT = { BJ: "小王", RJ: "大王" };

/** 解析一张牌 ID。非法输入返回 null（调用方跳过，绝不猜）。 */
export function parseCard(card) {
  const id = String(card == null ? "" : card).trim().toUpperCase();
  if (!id) return null;
  if (id === "BJ" || id === "RJ") {
    return {
      id,
      suit: "",
      suitSymbol: "",
      suitName: "",
      rank: id,
      rankText: RANK_TEXT[id],
      label: RANK_TEXT[id],
      red: id === "RJ",
      joker: true,
      order: RANK_ORDER[id],
    };
  }
  const m = /^([SHCD])(10|[2-9]|[JQKA])$/.exec(id);
  if (!m) return null;
  const suit = m[1];
  const rank = m[2];
  return {
    id,
    suit,
    suitSymbol: SUIT_SYMBOLS[suit] || "",
    suitName: SUIT_NAMES[suit] || "",
    rank,
    rankText: rank,
    label: `${SUIT_SYMBOLS[suit] || ""}${rank}`,
    red: suit === "H" || suit === "D",
    joker: false,
    order: RANK_ORDER[rank] || 0,
  };
}

/** 牌面展示用的短标签：♠3 / ♥10 / 大王。脏值原样返回安全文本。 */
export function cardLabel(card) {
  const c = parseCard(card);
  return c ? c.label : String(card == null ? "" : card);
}

/** 手牌排序：点数升序，同点按 ♠♥♣♦；王最大。返回新数组，不改入参。 */
export function sortCards(cards) {
  return (Array.isArray(cards) ? cards : [])
    .map((raw) => ({ raw, card: parseCard(raw) }))
    .filter((x) => x.card)
    .sort((a, b) => {
      if (a.card.order !== b.card.order) return a.card.order - b.card.order;
      return (SUIT_ORDER[a.card.suit] ?? 9) - (SUIT_ORDER[b.card.suit] ?? 9);
    })
    .map((x) => x.raw);
}

/** 手牌分组行（窄屏可多行折叠）：最多 maxPerRow 张一行，保持排序。
 * 20 张在 375px 下不会横向溢出；行数由牌数决定，不做规则判断。 */
export function handRows(cards, maxPerRow = 10) {
  const per = Math.max(1, Math.trunc(Number(maxPerRow) || 10));
  const sorted = sortCards(cards);
  const rows = [];
  for (let i = 0; i < sorted.length; i += per) rows.push(sorted.slice(i, i + per));
  return rows;
}

/** 单张牌提交文本：点数（王保持 BJ/RJ，服务端宽容解析）。 */
export function cardMoveText(card) {
  const c = parseCard(card);
  if (!c) return "";
  return c.joker ? c.id : c.rank;
}

/** 点选集合 → move 字符串（点数升序、空格分隔）。空选择返回空串。 */
export function selectionMove(cards) {
  const list = (Array.isArray(cards) ? cards : []).filter((c) => parseCard(c));
  if (!list.length) return "";
  return sortCards(list).map(cardMoveText).join(" ");
}

/** 切换一张牌的选中态（返回新 Set；绝不改动入参）。 */
export function toggleSelected(selected, card) {
  const next = new Set(selected || []);
  const id = String(card == null ? "" : card).trim().toUpperCase();
  if (!id) return next;
  if (next.has(id)) next.delete(id);
  else next.add(id);
  return next;
}

/** 只保留仍在手牌里的选中项（服务端推进/换局后不残留死牌）。 */
export function pruneSelection(selected, hand) {
  const have = new Set((Array.isArray(hand) ? hand : []).map((c) => String(c).trim().toUpperCase()));
  const next = new Set();
  (selected || new Set()).forEach((id) => {
    if (have.has(id)) next.add(id);
  });
  return next;
}

/* ---------------- 视角 / 回合 ---------------- */

/** 本人座位（0 起；null=观战）。view.you 显式给出即定案（null 不被座位号翻案）。 */
export function youOf(view, match) {
  if (view && Object.prototype.hasOwnProperty.call(view, "you")) {
    if (view.you == null || view.you === "") return null;
    const n = Number(view.you);
    return Number.isInteger(n) && n >= 0 ? n : null;
  }
  const me = match && match.me;
  return Number.isInteger(me) && me >= 0 ? me : null;
}

export function isSpectator(view, match) {
  return youOf(view, match) == null;
}

/** 是否轮到我：view.myTurn 优先（服务端唯一真相），缺失时才按 turn/you 回填。 */
export function myTurnOf(view, you = null) {
  if (!view || typeof view !== "object") return false;
  if (typeof view.myTurn === "boolean") return view.myTurn;
  const me = Number.isInteger(you) ? you : youOf(view, null);
  if (me == null) return false;
  return Number(view.turn) === me;
}

export function phaseOf(view) {
  const p = String((view && view.phase) || "").trim().toLowerCase();
  return p === "bid" || p === "play" || p === "over" ? p : "";
}

/* ---------------- 叫分 ---------------- */

/** 服务端下发的合法叫分（0=不叫，1/2/3 分）。脏值丢弃、去重、升序。 */
export function legalBidChoices(view) {
  if (!view || phaseOf(view) !== "bid") return [];
  const raw = Array.isArray(view.legalBids) ? view.legalBids : [];
  const out = [];
  for (const v of raw) {
    const n = Number(v);
    if (!Number.isInteger(n) || n < 0 || n > 3 || out.includes(n)) continue;
    out.push(n);
  }
  return out.sort((a, b) => a - b);
}

export function bidLabel(bid) {
  const n = Number(bid);
  return n === 0 ? "不叫" : `${n} 分`;
}

/** 叫分按钮 → move：0 用 "pass"，其余用数字文本。 */
export function bidMove(bid) {
  const n = Number(bid);
  if (!Number.isInteger(n) || n < 0 || n > 3) return "";
  return n === 0 ? "pass" : String(n);
}

/* ---------------- 提示（唯一来源 view.hints） ---------------- */

/** 归一化 hints：只认带可读 move/name 的条目，最多 60 条（与服务端上限一致）。 */
export function hintsOf(view) {
  const raw = view && Array.isArray(view.hints) ? view.hints : [];
  const out = [];
  for (const h of raw.slice(0, 60)) {
    if (!h || typeof h !== "object") continue;
    const move = String(h.move == null ? "" : h.move).trim();
    const cards = Array.isArray(h.cards) ? h.cards.filter((c) => parseCard(c)) : [];
    if (!move && !cards.length) continue;
    out.push({
      cards,
      combo: h.combo && typeof h.combo === "object" ? h.combo : null,
      move: move || selectionMove(cards),
      name: String(h.name == null ? "" : h.name).trim() || "提示",
    });
  }
  return out;
}

/** 提示对应的选中牌：优先 cards（服务端已给出具体牌），缺失则无法点选，返回空集。 */
export function hintSelection(hint, hand) {
  const cards = hint && Array.isArray(hint.cards) ? hint.cards : [];
  if (!cards.length) return new Set();
  return pruneSelection(new Set(cards.map((c) => String(c).toUpperCase())), hand);
}

/** 提示轮换索引：count<=0 返回 -1；否则循环前进。 */
export function nextHintIndex(current, count) {
  const n = Math.max(0, Math.trunc(Number(count) || 0));
  if (!n) return -1;
  const cur = Number.isInteger(current) ? current : -1;
  return (cur + 1) % n;
}

/* ---------------- 局面文案 ---------------- */

/** 座位阵营：叫分中 / 地主 / 农民。地主确定前不臆测。 */
export function seatRoleLabel(view, seat) {
  if (!view || Number(seat) !== Number(seat) || seat == null) return "";
  const idx = Number(seat);
  const landlord = Number.isInteger(Number(view.landlord)) && view.landlord != null ? Number(view.landlord) : null;
  if (landlord == null) return phaseOf(view) === "bid" ? "叫分中" : "";
  return landlord === idx ? "地主" : "农民";
}

export function seatRoleClass(view, seat) {
  const label = seatRoleLabel(view, seat);
  return label === "地主" ? "landlord" : label === "农民" ? "farmer" : "bidding";
}

/** 各座位剩余张数（公开信息）；缺失为 null，不猜。 */
export function countOf(view, seat) {
  const counts = view && Array.isArray(view.counts) ? view.counts : [];
  const n = Number(counts[seat]);
  return Number.isInteger(n) && n >= 0 ? n : null;
}

/** 底牌：地主确定前 null（服务端未公开）；已公开数组照常展示。 */
export function bottomCards(view) {
  const b = view && Array.isArray(view.bottom) ? view.bottom : [];
  return b.filter((c) => parseCard(c));
}

export function isBottomHidden(view) {
  return !bottomCards(view).length;
}

export function multiplierText(view) {
  const raw = Number(view && view.multiplier);
  if (Number.isInteger(raw) && raw > 0) return `${raw} 倍`;
  const bombs = Number(view && view.bombs);
  if (Number.isInteger(bombs) && bombs > 0) return `${2 ** bombs} 倍`;
  return "1 倍";
}

export function handNoText(view) {
  const no = Math.max(1, Math.trunc(Number(view && view.handNo) || 1));
  const hands = Math.max(1, Math.trunc(Number(view && view.hands) || 1));
  return hands > 1 ? `第 ${no}/${hands} 局` : `第 ${no} 局`;
}

export function bombsText(view) {
  const n = Math.trunc(Number(view && view.bombs) || 0);
  return n > 0 ? `炸弹 ×${n}` : "";
}

/** 桌面中央：当前需要压的牌（trick）或行动提示。 */
export function trickInfo(view) {
  const t = view && view.trick;
  if (!t || typeof t !== "object") return null;
  const cards = Array.isArray(t.cards) ? t.cards.filter((c) => parseCard(c)) : [];
  const name = String(t.name == null ? "" : t.name).trim();
  if (!cards.length && !name) return null;
  return { seat: Number.isInteger(Number(t.seat)) ? Number(t.seat) : null, cards, name: name || "上一手" };
}

export function needsText(view, you) {
  const phase = phaseOf(view);
  if (phase === "over") return "本局结束";
  if (phase === "bid") return myTurnOf(view, you) ? "轮到你叫分" : "等待其他玩家叫分";
  const t = trickInfo(view);
  if (t) return `需要压：${t.name}`;
  return myTurnOf(view, you) ? "你先出牌" : "等待其他玩家出牌";
}

/* ---------------- 最近动作 ---------------- */

/** 动作牌数组（cards:null=过/不叫；服务端显式 null，不臆造牌）。 */
export function actionCards(action) {
  const cards = action && Array.isArray(action.cards) ? action.cards : [];
  return cards.filter((c) => parseCard(c));
}

export function isPassAction(action) {
  if (!action || typeof action !== "object") return true;
  return actionCards(action).length === 0;
}

export function actionName(action) {
  if (!action || typeof action !== "object") return "过";
  const name = String(action.name == null ? "" : action.name).trim();
  if (name) return name;
  return isPassAction(action) ? "过" : "出牌";
}

/** 最近动作（最新在前，最多 limit 条）。recent 优先，缺失回落 latest 单条。 */
export function recentActions(view, limit = 4) {
  const list = view && Array.isArray(view.recent) ? view.recent.slice(-Math.max(1, limit)) : [];
  if (list.length) return list.slice().reverse();
  // 后端 latest 是「按座位的最新动作」数组（不是时间序列）；没有 recent 时
  // 不臆造顺序，宁可留空也不把三家动作排错。
  if (view && view.latest && typeof view.latest === "object" && !Array.isArray(view.latest)) {
    return [view.latest];
  }
  return [];
}

/* ---------------- 终局结算 ---------------- */

export function lastResult(view) {
  const arr = view && Array.isArray(view.results) ? view.results : [];
  for (let i = arr.length - 1; i >= 0; i -= 1) {
    if (arr[i] && typeof arr[i] === "object") return arr[i];
  }
  return null;
}

/** 单局结算标签：春天 / 反春 / 炸弹倍数（只陈述事实，不评价）。 */
export function resultTags(result) {
  const tags = [];
  const r = result && typeof result === "object" ? result : {};
  if (r.spring === true) tags.push("春天");
  if (r.antiSpring === true) tags.push("反春");
  const bombs = Math.trunc(Number(r.bombs) || 0);
  if (bombs > 0) tags.push(`炸弹 ×${bombs}`);
  const bid = Math.trunc(Number(r.bid) || 0);
  if (bid > 0) tags.push(`底分 ${bid}`);
  return tags;
}

export function resultTitle(result) {
  if (!result || typeof result !== "object") return "本局结束";
  if (result.landlordWon === true) return "地主胜";
  if (result.landlordWon === false) return "农民胜";
  return "本局结束";
}

export function scoresOf(view) {
  const raw = view && Array.isArray(view.scores) ? view.scores : [];
  return raw.map((v) => {
    const n = Number(v);
    return Number.isFinite(n) ? Math.trunc(n) : 0;
  });
}

export function deltasOf(result) {
  const raw = result && Array.isArray(result.deltas) ? result.deltas : [];
  return raw.map((v) => {
    const n = Number(v);
    return Number.isFinite(n) ? Math.trunc(n) : 0;
  });
}

/* ---------------- 走子记录文案 ---------------- */

const PASS_WORDS = /^(pass|不要|不叫|要不起|过|不出)$/i;

function tokenText(token) {
  const raw = String(token == null ? "" : token).trim();
  if (!raw) return "";
  const c = parseCard(raw);
  if (c) return c.rankText;
  if (raw === "小王" || raw === "大王") return raw;
  return raw;
}

/** 历史/记录里的 move → 人话（只做展示，不改变提交格式）。 */
export function moveText(move) {
  const t = String(move == null ? "" : move).trim();
  if (!t) return "";
  if (PASS_WORDS.test(t)) return t.toLowerCase() === "pass" ? "过" : t;
  const bid = /^bid\s*([0-3])$/i.exec(t);
  if (bid) {
    const n = Number(bid[1]);
    return n === 0 ? "不叫" : `叫 ${bidLabel(n)}`;
  }
  const tokens = t.split(/[\s,，、]+/).filter(Boolean);
  if (tokens.length && tokens.every((x) => parseCard(x) || /^(BJ|RJ|小王|大王)$/.test(x))) {
    return tokens.map(tokenText).join(" ");
  }
  return t;
}

/* ---------------- 操作闸门（页面与测试共用） ---------------- */

/** 出牌按钮是否可用：本人回合 + 已选牌 + 未忙 + 未终局 + 有操作权。 */
export function canSubmitPlay(view, selected, { canAct = false, busy = false, you = null } = {}) {
  if (phaseOf(view) !== "play" || busy || !canAct) return false;
  if (!myTurnOf(view, you)) return false;
  return (selected ? selected.size : 0) > 0;
}

/** 「不要」是否显示：只有服务端 canPass=true 且本人回合时。 */
export function passVisible(view, { canAct = false, busy = false, you = null } = {}) {
  if (phaseOf(view) !== "play" || busy || !canAct) return false;
  if (!myTurnOf(view, you)) return false;
  return view && view.canPass === true;
}