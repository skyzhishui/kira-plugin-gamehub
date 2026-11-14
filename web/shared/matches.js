/* matches.js — 大厅「进行中的棋局」统一操作台纯逻辑层（M8）。
 *
 * 后端契约（fixer 同步实施中，前端对旧后端缺字段完全兼容）：
 * - GET /games → { games, current, active_matches: [...] }
 *   active_matches[i] = {
 *     id, kind, game_name,
 *     players: [{ seat, kind: "human"|"ai"|"bot", actor, name }],
 *     source: { kind: "webui"|"chat", platform, session_id },
 *     status, move_count, turn_text,
 *     notice_target: { session_id, platform, status, error },
 *     can_control,
 *   }
 * - GET /matches/{id}/control（插件 api 前缀 + 宿主 JWT）→
 *   { match_id, seat, token, url }；终局 / 无人类座位后端拒绝。
 *
 * 设计约束（单测锁定，见 tests/active-matches.test.js）：
 * - 玩家一律以「对局 ID + 座位号」为 key，绝不用名字（同名可区分）；
 * - 展示映射 name + actor(user_id)，同名靠 user_id 区分；
 * - 陪练局（mode_options.mode=bot）：人设是队友不冒充对手；对手座位展示
 *   服务端真实名字（agent 配置名），名字缺席才兜底「陪练」；
 * - 进入操作 = control 端点拿 human token → sessionStorage → 跳 match；
 *   不修改页面 identity、不迁移 actor、口令不进链接（忽略响应 url 字段）；
 * - 禁止 AI/bot 座位：至少要有一个人类座位才可接管。
 *
 * 本模块零浏览器全局依赖（Node --test 直测）；IO 一律由调用方注入。
 */

import { normalizeMode, driverOf, getPersonaName } from "./mode.js";

export const GAME_NAMES = { gomoku: "五子棋", reversi: "黑白棋", doudizhu: "斗地主", chess: "国际象棋" };

const SEAT_NAMES = ["黑方", "白方"];

function txt(v) {
  return String(v ?? "").trim();
}

function intOrNull(v) {
  const n = Number(v);
  return Number.isFinite(n) ? Math.trunc(n) : null;
}

/* ---------------- 解析 / 归一化 ---------------- */

/** /games 响应 → 归一化对局列表。接受整体响应对象或裸数组；
 * 缺字段 / 旧后端 / 脏数据一律安全降级（无 id 的条目丢弃）。 */
export function parseActiveMatches(data) {
  const arr = Array.isArray(data)
    ? data
    : data && Array.isArray(data.active_matches)
      ? data.active_matches
      : [];
  const out = [];
  for (const raw of arr) {
    const m = normalizeActiveMatch(raw);
    if (m) out.push(m);
  }
  return out;
}

export function normalizeActiveMatch(raw) {
  if (!raw || typeof raw !== "object") return null;
  const id = txt(raw.id);
  if (!id) return null;
  const kind = txt(raw.kind);
  const mo = raw.mode_options && typeof raw.mode_options === "object" ? raw.mode_options : null;
  const drivers = mo && Array.isArray(mo.seat_drivers) ? mo.seat_drivers : [];
  const players = (Array.isArray(raw.players) ? raw.players : [])
    .map((p, i) => normalizePlayer(p, i, drivers[i]))
    .filter(Boolean);
  // 模式（S2-S4）：陪练局里人设是队友，对手座位（含 advanced 的 ai 座位）
  // 优先展示服务端下发的真实名字（agent 配置名）；只有名字缺席的非人类
  // 座位才兜底「陪练」。头像归属仍由页面按模式决定，不让人设冒充对手。
  const mode = normalizeMode(mo && mo.mode);
  const practice = mode === "practice";
  // 旧数据没有逐座位 driver：按模式级驱动回填（advanced=agent / easy=bot），
  // 与后端 modes.normalize_mode_options 的推断一致。
  const fallbackDriver = practice ? driverOf(raw) : "";
  const lineup = txt(mo && mo.lineup).toLowerCase();
  const noriRole = txt(mo && mo.nori_role).toLowerCase();
  const coach = noriRole === "coach" || (practice && !lineup);
  const shownPlayers = players.map((p) => {
    if (p.human || p.named) return p;
    const driver = p.driver || fallbackDriver;
    if (driver === "turn") return { ...p, name: getPersonaName() };
    if (driver === "agent") return { ...p, name: "对手" };
    if (driver === "bot") return { ...p, name: "陪练" };
    return { ...p, name: practice ? "陪练" : p.name };
  });
  return {
    id,
    kind,
    gameName: txt(raw.game_name) || GAME_NAMES[kind] || kind || "对局",
    mode,
    practice,
    coach,
    lineup,
    noriRole: noriRole === "player" || noriRole === "coach" ? noriRole : "",
    players: shownPlayers,
    source: normalizeSource(raw.source),
    status: normalizeStatus(raw.status),
    moveCount: intOrNull(raw.move_count),
    turnText: txt(raw.turn_text),
    notice: normalizeNotice(raw.notice_target),
    canControl: raw.can_control === true,
  };
}

function normalizePlayer(p, fallbackSeat, driver = "") {
  if (!p || typeof p !== "object") return null;
  const seat = intOrNull(p.seat) ?? fallbackSeat;
  const kind = txt(p.kind) || "human";
  const actor = txt(p.actor);
  const rawName = txt(p.name);
  const name = rawName || kindLabel(kind) || SEAT_NAMES[seat] || `座位 ${seat + 1}`;
  const d = String(driver == null ? "" : driver).trim().toLowerCase();
  return {
    seat,
    kind,
    actor,
    name,
    named: !!rawName,
    human: kind === "human",
    driver: d === "turn" || d === "agent" || d === "bot" ? d : "",
  };
}

function normalizeSource(s) {
  const kind = txt(s && s.kind);
  return {
    kind,
    platform: txt(s && s.platform),
    sessionId: txt(s && s.session_id),
  };
}

/* status 兼容两种形态：MatchView 的 {outcome, resultText,...} 或简字符串。
 * over 仅作前端防御（can_control 与后端终局拒绝仍是权威）。 */
function normalizeStatus(s) {
  if (s && typeof s === "object") {
    return { over: txt(s.outcome) !== "", resultText: txt(s.resultText) };
  }
  const t = txt(s);
  return { over: t !== "" && stringLooksOver(t), resultText: t };
}

const OVER_STATUS_WORDS = ["finish", "over", "end", "done", "complete"];

function stringLooksOver(t) {
  const low = t.toLowerCase();
  return OVER_STATUS_WORDS.some((w) => low.includes(w)) || /(结束|胜|负|和棋|平局)/.test(t);
}

function normalizeNotice(nt) {
  if (!nt || typeof nt !== "object") {
    return { sessionId: "", platform: "", status: "", error: "", ready: false };
  }
  const sessionId = txt(nt.session_id);
  const platform = txt(nt.platform);
  const status = txt(nt.status);
  const error = txt(nt.error);
  return { sessionId, platform, status, error, ready: !!(sessionId || platform || status) };
}

/* ---------------- 展示文案 ---------------- */

export function kindLabel(kind, practice = false) {
  if (kind === "ai") return practice ? "对手" : getPersonaName();
  return kind === "bot" ? "陪练" : "";
}

export function sourceLabel(source) {
  if (!source) return "未知来源";
  if (source.kind === "webui") return "网页开局";
  if (source.kind === "chat") {
    return source.platform ? `聊天开局 · ${source.platform}` : "聊天开局";
  }
  return source.kind ? `来源：${source.kind}` : "未知来源";
}

const NOTICE_STATUS_TEXT = {
  match_source: "跟随开局会话",
  default: "默认",
  override: "已指定",
  invalid: "无效",
  unavailable: "不可用",
};

export function noticeStatusText(status) {
  return NOTICE_STATUS_TEXT[status] || status;
}

/** notice_target → 展示摘要：平台 + 诊断状态 + 错误；整体未就绪给固定文案。 */
export function noticeSummary(notice) {
  if (!notice || !notice.ready) {
    return { ready: false, platform: "", statusText: "", error: "" };
  }
  return {
    ready: true,
    platform: notice.platform || "未知平台",
    statusText: noticeStatusText(notice.status),
    error: notice.error,
  };
}

/** 轮次行：手数 + 后端轮次文案；皆缺时回落结果/等待。 */
export function turnSummary(m) {
  const parts = [];
  if (m && m.moveCount != null && m.moveCount > 0) parts.push(`第 ${m.moveCount} 手`);
  if (m && m.turnText) parts.push(m.turnText);
  if (!parts.length) parts.push((m && m.status && m.status.resultText) || "等待开局");
  return parts.join(" · ");
}

/* ---------------- 判定 ---------------- */

/** 玩家唯一 key：对局 ID + 座位号。同名玩家/同座位名绝不共享 key。 */
export function playerKey(matchId, player) {
  return `${txt(matchId)}#${player && player.seat != null ? player.seat : "?"}`;
}

/** 可接管 = 后端 can_control 且未终局且存在人类座位（禁 AI/bot 座位）。 */
export function isControllable(m) {
  if (!m || m.canControl !== true) return false;
  if (m.status.over) return false; // 终局不准 control（后端同样拒绝，前端防御）
  return m.players.some((p) => p.human);
}

/** 不可接管的展示原因（按「已结束 → AI 对局 → 暂不可接管」优先级）。 */
export function controlBlockedReason(m) {
  if (m && m.status && m.status.over) return "已结束";
  if (!m || !m.players.some((p) => p.human)) return "无人类玩家座位";
  return "暂不可接管";
}

/* ---------------- 跳转 / 进入操作 ---------------- */

/** match 页地址；口令永不进链接（token 存 sessionStorage 后由 match 页读取）。 */
export function matchHref(matchId) {
  return `./match.html?id=${encodeURIComponent(txt(matchId))}`;
}

/**
 * 「进入操作」统一流程：
 * 1) GET /matches/{id}/control（宿主 auth）拿 human token；
 * 2) token 写 sessionStorage["gamehub_match_<match_id>"]（响应的 url 字段
 *    明确不用——带口令的链接违背「口令不进链接」约定）；
 * 3) 跳 ./match.html?id=<match_id>（以响应 match_id 为准）。
 * 不调用 identity、不迁移 actor；失败原样上抛，由调用方恢复按钮重试。
 * io 全部注入：{ api, writeToken, navigate }，Node 可直测。
 */
export async function enterControl(matchId, io = {}) {
  const apiFn = io.api;
  const writeToken = io.writeToken || (() => {});
  const navigate = io.navigate || (() => {});
  const id = txt(matchId);
  if (!id) throw new Error("缺少对局 ID，无法接管");
  if (typeof apiFn !== "function") throw new Error("enterControl 缺少 api 注入");
  const data = await apiFn(`/matches/${encodeURIComponent(id)}/control`);
  const rid = txt(data && data.match_id) || id;
  const token = txt(data && data.token);
  if (!token) throw new Error("没有拿到操作口令，请稍后重试");
  writeToken(rid, token);
  navigate(matchHref(rid));
  return rid;
}
