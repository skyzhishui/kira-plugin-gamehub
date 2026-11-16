/* mode.js — S2–S4 对局模式 + 人设代下 · 纯逻辑适配层（Node 可直测，零浏览器依赖）。
 *
 * 职责（唯一契约适配点）：
 * - 开局载荷构造：createMatchPayload（与人设对战 / 与人设进阶对战 / 与陪练）
 * - 三档等级：normalizeLevel / levelOf / levelVisible（初级/中级/高级，默认中级）
 * - MatchView 字段容错读取：modeOf（mode/difficulty）、delegatedOf（代下态）
 * - 代下动作体：delegateAction；重开载荷：restartPayload；重开按钮文案：restartLabel
 * - 座位展示名：seatDisplay（优先服务端真实名字；陪练局无名字的非人类座位
 *   兜底「陪练」，绝不冒充宿主主 agent）
 * - 用户文案常量：对手三口径 + 三档等级（用户可见文案不出现 AI 字样）
 *
 * 后端规范（kira-plugin-gamehub/modes.py，协议与 nori 原版一致）：
 *   mode_options = { mode:"nori"|"bot", role:"opponent"|"teammate",
 *                    driver:"turn"|"agent"|"bot", difficulty:""|"easy"|"advanced",
 *                    level:"beginner"|"intermediate"|"advanced", delegation:bool }
 * 等级对人设本人与进阶对手通用（driver=turn/agent）；陪练（driver=bot）
 * 不显示三档。旧 difficulty 仅保留驱动兼容：easy=陪练、advanced=进阶对手。
 * 陪练局（bot/teammate）对手座位：陪练=bot 座位、进阶对手=ai 座位。后端会为
 * practice 的 ai 座位下发实际配置的名字——前端必须原样展示，不得改写成
 * 「陪练」或人设名；只有名字缺席的非人类座位才兜底「陪练」。
 * 头像归属仍按模式：陪练局人设是队友，对手位绝不挂人设头像。
 *
 * game_agent 名可配置（KiraAI）：所有指代「可配置人设」的文案统一走
 * personaNameText / setPersonaName / personaText —— 配置名留空或就是 "nori"
 * 时保留配置标识 "nori"，其它取值原样渲染。UI 在大厅/对局页拿到
 * /games 或 /ui-config 的 agent_name 后调用一次 setPersonaName 即全局生效；
 * 纯函数默认值仍是配置标识 "nori"，Node 测试无需初始化。
 *
 * 设计约束：
 * - 旧后端/旧对局（缺字段）安全降级：mode 为空 → 沿用旧契约，不破坏现有流程
 * - 后端最终字段名变化时只改本文件（match.js/hub.js 不感知字段位置）
 */

/* ---------------- 文案常量（用户可见，无 AI 字样） ----------------
 * 对手三种口径：
 *   nori  人设本人（行动通知回合驱动）
 *   agent 进阶对手（game_agent 驱动，同一个可配置人设）
 *   bot   陪练（纯规则，不占座位地陪打）
 * 三档等级只对 nori/agent 有效：初级不做威胁分析、中级带坐标、高级另附
 * 机器人前三候选。等级不是棋力承诺，文案不写「搜索最优」。
 * MODE_LABELS / MODE_HINTS 是静态模板（导出给需要模板的调用方）；
 * 界面请用下面的 modeLabel / modeHint，它们跟随 setHostAgentName / setPersonaName 生效。 */

/** 宿主 AI 名字的占位记号。文案模板里写它，渲染时替换成 /ui-config 下发的
 * host_agent_name（宿主当前激活人设的名字）；只有在拿不到宿主名字时才原样
 * 显示「AI」，绝不在界面上写死任何身份词。 */
export const HOST_PLACEHOLDER = "AI";

/** 静态模板：宿主名字用 HOST_PLACEHOLDER 占位，渲染时换成真实名字。
 * 界面一律走下面的 modeLabel()（已替换好）；这里的常量只给需要模板的调用方，
 * 绝不把「与AI对战」这种写死身份的串直接上屏。 */
export const MODE_LABELS = {
  nori: `与${HOST_PLACEHOLDER}对战`,
  agent: "与搭档对战",
  bot: "与陪练对战",
};

/** game_agent 的缺省配置名（schema.json 的 agent_name 默认值）。
 * 它只是一个配置标识；渲染文案由服务端下发的名字决定，前端不写死任何人设名。 */
const AGENT_CONFIG_DEFAULT = "nori";

let personaName = AGENT_CONFIG_DEFAULT;

/** Host AI display name (the seat driven by action notices). Empty until
 * ``setHostAgentName`` runs; falls back to a neutral「AI」so the copy never
 * claims a specific persona before /ui-config answers. */
let hostAgentName = "";

export function setHostAgentName(v) {
  hostAgentName = String(v == null ? "" : v).trim();
  return hostAgentName;
}

export function getHostAgentName() {
  return hostAgentName || HOST_PLACEHOLDER;
}

/** 配置里的 game_agent 名 → 渲染名。
 * 前端不再自带中文缺省名：``nori`` 这类配置标识原样返回（后端目录项
 * 已经下发渲染后的 label），空值回落中性「搭档」。 */
export function personaNameText(v) {
  const s = String(v == null ? "" : v).trim();
  if (!s || s.toLowerCase() === AGENT_CONFIG_DEFAULT) return AGENT_CONFIG_DEFAULT;
  return s;
}

/** 记录本次页面会话的人设名（/games 或 /ui-config 的 agent_name）。 */
export function setPersonaName(v) {
  personaName = personaNameText(v);
  return personaName;
}

export function getPersonaName() {
  return personaName;
}

/** 把人设模板文案里的配置标识渲染成当前 game_agent 名。 */
export function personaText(template) {
  return String(template == null ? "" : template).replaceAll(AGENT_CONFIG_DEFAULT, personaName);
}

export const LEVEL_VALUES = ["beginner", "intermediate", "advanced"];
export const LEVEL_LABELS = { beginner: "初级", intermediate: "中级", advanced: "高级" };
export const LEVEL_HINTS = {
  beginner: "轻松的切磋",
  intermediate: "会认真应战。",
  advanced: "会全力以赴。",
};

/** 进阶对手的公开名（服务端 agent_name）；缺席用中性「玩家」，不写死人设。 */
export function agentNameText(v) {
  const s = String(v == null ? "" : v).trim();
  return s || "玩家";
}

/** 陪练显示名（服务端 practice_name，默认「小白」）；缺席用「陪练」。 */
export function practiceNameText(v) {
  const s = String(v == null ? "" : v).trim();
  return s || "陪练";
}

export function modeLabel(mode, agentName) {
  if (mode === "agent") return `与${agentNameText(agentName)}对战`;
  // mode "nori" = the host AI itself (action-notice driven), not the
  // plugin-owned game_agent persona.
  if (mode === "nori") return `与${getHostAgentName()}对战`;
  if (mode === "bot") return "与陪练对战";
  return "";
}

/** 三档等级的态度文案：beginner=轻松的切磋；其余为对手应战态度。
 * name 为对手名字（人设名 / 进阶对手名 / 陪练名），缺席回落中性称呼。 */
export function levelHint(level, name) {
  const who = String(name == null ? "" : name).trim() || "对手";
  const lv = normalizeLevel(level) || "intermediate";
  if (lv === "beginner") return `与${who}轻松的切磋`;
  if (lv === "advanced") return `${who}会全力以赴。`;
  return `${who}会认真应战。`;
}

/** 把文案模板里的宿主占位记号换成宿主真名。
 *
 * 只在「独立出现」时替换：``AI和搭档`` 会换，``KAI`` 这种真实名字里的
 * 子串不会被误伤（宿主/搭档名字完全由用户配置，不能假定字符集）。 */
function substituteHostPlaceholder(text, hostName) {
  const host = String(hostName == null ? "" : hostName);
  if (!host) return text;
  return text.replace(/(^|[^A-Za-z])AI(?![A-Za-z])/g, (_m, prefix) => `${prefix}${host}`);
}

export const MODE_HINTS = {
  nori: "AI会认真应战。",
  agent: "会认真应战。",
  bot: "轻松的切磋",
};

export const MATE_NOTE_TEXT = "聊天里给你支招，也能替你落子";

/* ---------------- 三人桌阵容（斗地主等 3 座位游戏） ----------------
 * 后端契约（modes.py，与 nori 原版一致）：
 *   mode_options = { schema:2, lineup,
 *                    seat_drivers:["human"|"turn"|"agent"|"bot", ...]  // 全 3 席对齐
 *                    nori_seat: 1|null, nori_role:"player"|"coach", delegation:bool }
 * lineup 是入口，其余是派生值；座位身份一律按 seat_drivers 判定，不按名字猜。
 * 四阵容（用户口径）：
 *   nori_agent 人设参战（座位 1）+ 进阶对手（座位 2）
 *   nori_bot   人设参战（座位 1）+ 陪练（座位 2）
 *   agent_bot  人设不占座（队友/教练）+ 进阶对手 + 陪练
 *   bot_bot    人设不占座（队友/教练）+ 两位陪练
 * 2 座位棋类永远不写这些键，本段新增函数对旧数据一律降级为空值。 */

/** 旧后端（目录未下发 lineups）的回落文案。宿主名/搭档名在渲染时替换，
 * 这里只留占位词，前端不写死任何人设名。 */
export const LINEUP_LABELS = {
  nori_agent: "AI + 搭档",
  nori_bot: "AI + 陪练",
  agent_bot: "AI支招 · 搭档上桌",
  bot_bot: "AI支招 · 陪练上桌",
};

/* 地主/农民要等叫分结束才定，开局前同桌都是「同桌」——不预先指派谁和谁一队。
 * 「搭档」是进阶对手的占位称呼，渲染时优先换成服务端公开名（agent_name）。 */
export const LINEUP_HINTS = {
  nori_agent: "AI和搭档与你同桌；地主叫分后确定",
  nori_bot: "AI和陪练与你同桌；地主叫分后确定",
  agent_bot: "搭档和陪练与你同桌，AI在聊天里支招",
  bot_bot: "两位陪练与你同桌，AI在聊天里支招",
};

/* 全 3 席对齐（含人类座位 0）：seat_drivers 与 seats 一一对应，human 也写明。
 * 兼容旧计划的两项数组（["turn","agent"]，仅座位 1..n-1）：读取时按长度判别。 */
const LINEUP_PLANS = {
  nori_agent: { kinds: ["human", "ai", "ai"], drivers: ["human", "turn", "agent"], noriSeat: 1, noriRole: "player" },
  nori_bot: { kinds: ["human", "ai", "bot"], drivers: ["human", "turn", "bot"], noriSeat: 1, noriRole: "player" },
  agent_bot: { kinds: ["human", "ai", "bot"], drivers: ["human", "agent", "bot"], noriSeat: null, noriRole: "coach" },
  bot_bot: { kinds: ["human", "bot", "bot"], drivers: ["human", "bot", "bot"], noriSeat: null, noriRole: "coach" },
};

export const LINEUP_VALUES = Object.keys(LINEUP_PLANS);

export function normalizeLineup(v) {
  const s = String(v == null ? "" : v).trim().toLowerCase();
  return LINEUP_PLANS[s] ? s : "";
}

/** 目录 label → 用户可读文案：目录当前可能写「当前agent/陪练」，
 * 前端统一收敛为「搭档/陪练」，不把技术词带进大厅（更不出现 AI 字样）。
 * 身份口径：阵容里的「AI」是 turn 驱动的宿主主 agent → 渲染宿主名；
 * 「搭档」是 game_agent → 渲染 agentName（配置标识默认 nori）。 */
export function friendlyLineupLabel(label, fallback, agentName) {
  const raw = String(label == null ? "" : label).trim() || String(fallback == null ? "" : fallback).trim();
  if (!raw) return "";
  const name = String(agentName == null ? "" : agentName).trim() || "搭档";
  const host = getHostAgentName();
  // 占位符先落成不冲突的记号，避免「宿主名恰好等于搭档名」时被第二次替换：
  // 「AI」= turn 驱动的宿主主 agent；「搭档/当前agent」= game_agent。
  return substituteHostPlaceholder(
    raw
      .replace(/当前\s*agent/gi, "\u0000")
      .replace(/搭档/g, "\u0000")
      .replace(/\s*\+\s*/g, " + ")
      .replaceAll("\u0000", name),
    host
  ).trim();
}

/** 目录项 → 阵容选项 [{value,label}]。目录未声明（旧后端）回落内置四档文案。 */
export function lineupsOf(game, agentName) {
  const raw = game && typeof game === "object" ? game.lineups : null;
  const out = [];
  const seen = new Set();
  if (Array.isArray(raw)) {
    for (const item of raw) {
      const value = normalizeLineup(item && item.value);
      if (!value || seen.has(value)) continue;
      seen.add(value);
      const label = friendlyLineupLabel(item && item.label, LINEUP_LABELS[value], agentName);
      out.push({ value, label });
    }
  }
  if (out.length) return out;
  return LINEUP_VALUES.map((value) => ({ value, label: friendlyLineupLabel("", LINEUP_LABELS[value], agentName) }));
}

function lineupPlanOf(lineup) {
  const key = normalizeLineup(lineup);
  const plan = key ? LINEUP_PLANS[key] : null;
  return plan ? { lineup: key, ...plan } : null;
}

export function lineupHint(lineup, agentName) {
  const key = normalizeLineup(lineup) || "nori_agent";
  const raw = LINEUP_HINTS[key];
  // 「搭档」是 game_agent 的占位称呼：有配置名时换成名字，缺席保持中性称呼。
  const name = String(agentName == null ? "" : agentName).trim() || "搭档";
  const host = getHostAgentName();
  // 同 friendlyLineupLabel：先落搭档名，再替换宿主占位记号。
  return substituteHostPlaceholder(raw.replace(/搭档/g, name), host);
}

/** 当前对局的阵容（mode_options 首选；旧后端按座位构成无法可靠推断，返回空串）。 */
export function lineupOf(match) {
  const o = match && typeof match === "object" ? match : {};
  for (const src of [o.mode_options, o.options, o]) {
    if (!src || typeof src !== "object") continue;
    const v = normalizeLineup(src.lineup);
    if (v) return v;
  }
  return "";
}

/** 3 座位局判定：声明了阵容或座位数 > 2（不写死 kind）。 */
export function isThreeSeatMatch(match) {
  if (!match || typeof match !== "object") return false;
  if (lineupOf(match)) return true;
  return Array.isArray(match.seats) && match.seats.length > 2;
}

/** Current-turn seat (0-based; null = nobody waiting / match over).
 *
 * Shared by the lobby's "active matches" turn line and the match page status line:
 * the three-seat condition must exist only once, otherwise the two-seat/three-seat
 * boundary drifts between the two call sites again.
 * Source priority: status.waitingOn (number or single-element array) -> view.toPlay
 * (two-seat games) -> view.turn (three-seat games or a seat index >= 2).
 * At match end view.turn is null and ``+null === 0`` with ``Number.isInteger(0)``
 * true, so null has to be excluded explicitly; otherwise "nobody is waiting" is
 * misread as "waiting on seat 0". */
export function turnSeatOf(match) {
  if (!match || typeof match !== "object") return null;
  const st = match.status && typeof match.status === "object" ? match.status : {};
  let wo = typeof st.waitingOn === "number" ? st.waitingOn : null;
  if (wo == null && Array.isArray(st.waitingOn) && st.waitingOn.length === 1 && Number.isInteger(st.waitingOn[0])) {
    wo = st.waitingOn[0];
  }
  const v = match.view;
  if (wo == null && v && (+v.toPlay === 1 || +v.toPlay === 2)) wo = +v.toPlay - 1;
  if (wo == null && v && v.turn != null && Number.isInteger(+v.turn) && +v.turn >= 0 && (+v.turn >= 2 || isThreeSeatMatch(match))) {
    wo = +v.turn;
  }
  const seats = Array.isArray(match.seats) ? match.seats : [];
  if (!Number.isInteger(wo) || !seats[wo]) return null;
  return { index: wo, seat: seats[wo] };
}

/** 座位驱动方。三人桌 seat_drivers 与全部座位对齐（0=human）；
 * 兼容旧两项数组（仅座位 1..n-1 对齐）。2 座位旧局回退对局级 driver
 * （现行为逐字段不变）。 */
export function seatDriverOf(match, seatIndex) {
  if (!match || typeof match !== "object") return "";
  const idx = Number(seatIndex);
  if (!Number.isFinite(idx) || idx < 0) return "";
  const mo = match.mode_options && typeof match.mode_options === "object" ? match.mode_options : null;
  const drivers = mo && Array.isArray(mo.seat_drivers) ? mo.seat_drivers : null;
  if (drivers && drivers.length) {
    const full = drivers.length >= 3;
    const rawValue = full ? drivers[idx] : idx >= 1 ? drivers[idx - 1] : "";
    const raw = String(rawValue == null ? "" : rawValue).trim().toLowerCase();
    if (raw === "turn" || raw === "agent" || raw === "bot" || (raw === "human" && idx === 0)) return raw;
  }
  const plan = lineupPlanOf(lineupOf(match));
  if (plan) return plan.drivers[idx] || "";
  if (idx < 1) return "";
  return driverOf(match);
}

/** 宿主主 agent 所在座位（0 起）。规范字段 nori_seat 显式 null = 宿主主 agent 不占座（教练/队友）；字段值保持 nori。 */
export function noriSeatOf(match) {
  if (!match || typeof match !== "object") return null;
  const mo = match.mode_options && typeof match.mode_options === "object" ? match.mode_options : null;
  if (mo && Object.prototype.hasOwnProperty.call(mo, "nori_seat")) {
    if (mo.nori_seat == null || mo.nori_seat === "") return null;
    const n = Number(mo.nori_seat);
    return Number.isInteger(n) && n >= 0 ? n : null;
  }
  const plan = lineupPlanOf(lineupOf(match));
  if (plan) return plan.noriSeat;
  // 2 座位旧局：宿主主 agent 局的对家座位即宿主主 agent；陪练局不占座
  if (isPractice(match)) return null;
  return driverOf(match) === "turn" ? 1 : null;
}

export function noriRoleOf(match) {
  if (!match || typeof match !== "object") return "";
  const mo = match.mode_options && typeof match.mode_options === "object" ? match.mode_options : null;
  const raw = mo && typeof mo.nori_role === "string" ? mo.nori_role.trim().toLowerCase() : "";
  if (raw === "player" || raw === "coach") return raw;
  const plan = lineupPlanOf(lineupOf(match));
  if (plan) return plan.noriRole;
  return isPractice(match) ? "coach" : "player";
}

/** 宿主主 agent 是队友（教练）而不是同桌玩家：两座位陪练局与 C/D 阵容。 */
export function isCoachMatch(match) {
  return noriRoleOf(match) === "coach";
}

/** 一个座位的身份归属：human / nori / agent / bot。用于头像、气泡、身份片，
 * 绝不按名字猜（名字可以是任何配置）。 */
export function seatIdentity(match, seat, index) {
  if (!seat || typeof seat !== "object") return "";
  if (seat.kind === "human") return "human";
  const driver = seatDriverOf(match, index);
  if (driver === "turn") return "nori";
  if (driver === "agent") return "agent";
  if (driver === "bot") return "bot";
  if (seat.kind === "ai") return isCoachMatch(match) ? "agent" : "nori";
  return seat.kind === "bot" ? "bot" : "";
}

/** 身份小片（3 座位紧凑版）：驱动方决定归属，不误挂人设名。 */
export function seatIdentityLabel(match, seat, index) {
  if (!seat || seat.kind === "human") return "";
  const identity = seatIdentity(match, seat, index);
  if (identity === "nori") return getHostAgentName();
  if (identity === "agent") return "对手";
  if (identity === "bot") return "陪练";
  return "";
}

/** 三档等级归一：beginner/intermediate/advanced；缺省/脏值 → 中级（后端默认一致）。 */
export function normalizeLevel(v) {
  const s = String(v == null ? "" : v).trim().toLowerCase();
  return LEVEL_VALUES.includes(s) ? s : "";
}

/** 对手口径（nori/agent/bot）：由 mode + driver 判定，不看名字。
 * 与后端 modes.py 一致：bot 模式 + agent 驱动 = 当前 agent；其余 bot 驱动 = 陪练。 */
export function opponentKindOf(match) {
  const { mode } = modeOf(match);
  if (mode === "nori") return "nori";
  if (mode !== "practice") return "";
  const driver = driverOf(match);
  if (driver === "agent") return "agent";
  return "bot";
}

/** 三档等级是否对该局可见：对宿主主 agent 或当前 agent（含宿主主 agent 参战的 bot 模式）。
 * 陪练（easy / bot 驱动）不显示三档——它没有威胁分析。 */
export function levelVisible(match) {
  return opponentKindOf(match) !== "bot";
}

/** 对局的等级（MatchView.mode_options.level；缺省中级）。 */
export function levelOf(match) {
  if (!match || typeof match !== "object") return "intermediate";
  for (const src of [match.mode_options, match.options, match]) {
    if (!src || typeof src !== "object") continue;
    const v = normalizeLevel(src.level);
    if (v) return v;
  }
  return "intermediate";
}

/** 开局方式提示（对手三选一 + 三档等级）：宿主主 agent 应战 / 当前 agent 应战 /
 * 陪练按规则走子。level 只对 nori/agent 有意义，不影响文案承诺。 */
export function modeHint(mode, level, name) {
  if (mode === "agent") return MODE_HINTS.agent;
  if (mode === "bot") return MODE_HINTS.bot;
  return substituteHostPlaceholder(MODE_HINTS.nori, getHostAgentName());
}

/* ---------------- 归一化 ---------------- */

/** 旧目录/旧后端没有 supports_turn 时的保守回落：只认确知「按回合授权」的棋种。
 * 新增棋种应由目录字段（engine/games.catalog() → supports_turn）声明，不要在此
 * 追加字面量白名单；这里仅为兼容尚在迁移中的后端与旧测试 fixture。 */
const LEGACY_TURN_KINDS = new Set(["gomoku", "reversi"]);

/**
 * 是否支持「与宿主主 agent 对战 / 与陪练对战」模式选择（= 回合驱动棋种）。
 * 入参：目录项（推荐，读 supports_turn）或旧式 kind 字符串（回落白名单）。
 * 目录字段显式给出即定案（false 也定案）；缺字段再按 kind 保守回落。
 */
export function supportsModes(game) {
  if (game && typeof game === "object") {
    const flag = game.supports_turn;
    if (flag === true || flag === false) return flag;
    if (flag === 1 || flag === 0) return flag === 1;
    if (flag === "true" || flag === "false") return flag === "true";
    return LEGACY_TURN_KINDS.has(normalizeKind(game.kind));
  }
  return LEGACY_TURN_KINDS.has(normalizeKind(game));
}

function normalizeKind(v) {
  return String(v == null ? "" : v).trim().toLowerCase();
}

/** UI 内部只区分 nori（宿主主 agent 是对手）/ practice（宿主主 agent 是队友）；后端 "bot" 模式
 * 即陪练局（modes.MODE_BOT），一并归一为 "practice"。 */
export function normalizeMode(v) {
  const s = String(v == null ? "" : v).trim().toLowerCase();
  if (s === "nori") return "nori";
  if (s === "practice" || s === "bot") return "practice";
  return "";
}

export function normalizeDifficulty(v) {
  const s = String(v == null ? "" : v).trim().toLowerCase();
  return s === "easy" || s === "advanced" ? s : "";
}

/* ---------------- MatchView 容错读取 ---------------- */

/** 视图 → { mode, difficulty }。候选位：mode_options（后端规范）→ options
 * （gomoku_mode）→ 顶层 → view。候选位全缺 mode 时按座位构成回填，与后端
 * modes.normalize_mode_options 的 legacy 推断一致：bot 座位局（无 ai 座）是
 * 陪练局，ai 座位局按宿主主 agent 局口径（前端保持空串=未知，不显式断言 nori）。
 * 这样「mode 缺失但 driver=agent 已写入」的旧载荷也能拿到陪练语义。 */
export function modeOf(match) {
  const o = match && typeof match === "object" ? match : {};
  let mode = "";
  let difficulty = "";
  for (const src of [o.mode_options, o.options, o, o.view]) {
    if (!src || typeof src !== "object") continue;
    if (!mode) {
      mode = normalizeMode(
        src.mode != null ? src.mode : src.gomoku_mode
      );
    }
    if (!difficulty) difficulty = normalizeDifficulty(src.difficulty);
    if (mode && difficulty) break;
  }
  if (!mode) {
    const seats = Array.isArray(o.seats) ? o.seats : [];
    const seatKind = (s) => (s && typeof s === "object" ? String(s.kind || "") : "");
    const hasBot = seats.some((s) => seatKind(s) === "bot");
    const hasAi = seats.some((s) => seatKind(s) === "ai");
    if (hasBot && !hasAi) mode = "practice";
  }
  return { mode, difficulty };
}

export function isPractice(match) {
  return modeOf(match).mode === "practice";
}

/** 对手座位驱动方：turn（宿主主 agent 行动回合）/ agent（高级陪练）/ bot（简单陪练）。
 * 规范字段优先；旧对局缺 driver 时按难度回填（advanced=agent、其余=bot），
 * 与后端 modes.normalize_mode_options 的推断保持一致。 */
export function driverOf(match) {
  const { mode, difficulty } = modeOf(match);
  if (mode !== "practice") return "";
  const o = match && typeof match === "object" ? match : {};
  for (const src of [o.mode_options, o.options, o, o.view]) {
    if (!src || typeof src !== "object") continue;
    const d = String(src.driver == null ? "" : src.driver).trim().toLowerCase();
    if (d === "agent" || d === "bot" || d === "turn") return d;
  }
  return difficulty === "advanced" ? "agent" : "bot";
}

/* 座位头像（KiraAI 唯一允许的角色图）：插件自己的 agent-avatar。
 * 默认是随插件分发的 web/assets/agent-avatar.png（页面相对路径，静态托管在
 * /page/plugin/kira-plugin-gamehub/hub/ 下）；WebUI 上传后后端把它换成
 * /api/plugin/kira-plugin-gamehub/agent-avatar?v=<mtime>，由 /ui-config 的
 * agent_avatar 下发（host/avatar.py）。前端只读这个字段，不改素材文件。 */
export const AGENT_AVATAR_URL = "./assets/agent-avatar.png";

/** 允许渲染的头像地址：插件默认素材，或插件自己的 agent-avatar 端点。
 * 其它取值（含宿主人设图/外链/文件路径）一律回落默认，避免挂错图。 */
export function resolveAgentAvatar(value) {
  const s = String(value == null ? "" : value).trim();
  if (!s) return AGENT_AVATAR_URL;
  if (s === AGENT_AVATAR_URL) return s;
  if (s.startsWith(`${AGENT_AVATAR_URL}?`)) return s;
  if (/^\/api\/plugin\/kira-plugin-gamehub\/agent-avatar(\?.*)?$/.test(s)) return s;
  return AGENT_AVATAR_URL;
}

let agentAvatar = AGENT_AVATAR_URL;

/** 记录 /ui-config 下发的头像地址（脏值回落默认）。 */
export function setAgentAvatar(value) {
  agentAvatar = resolveAgentAvatar(value);
  return agentAvatar;
}

export function getAgentAvatar() {
  return agentAvatar;
}

/* 宿主主 agent 头像：当前激活人设的「自拍参考图」。
 * 后端通过 ctx.persona_mgr.get_reference_image() 取到文件，再由插件端点
 * /api/plugin/kira-plugin-gamehub/host-avatar?v=<mtime> 转发（宿主自己的
 * /api/personas/{id}/reference-image 需要 JWT，<img> 带不了这个头）。
 * 空串是有效状态：宿主没配自拍参考图 → 座位渲染名字首字占位，
 * 绝不借用 game_agent 的头像冒充主 agent。 */
const HOST_AVATAR_ENDPOINT = /^\/api\/plugin\/kira-plugin-gamehub\/host-avatar(\?.*)?$/;

function resolveHostAvatar(value) {
  const s = String(value == null ? "" : value).trim();
  return HOST_AVATAR_ENDPOINT.test(s) ? s : "";
}

let hostAvatar = "";

/** 记录 /ui-config 下发的宿主头像地址（脏值/缺席一律当作「没有」）。 */
export function setHostAvatar(value) {
  hostAvatar = resolveHostAvatar(value);
  return hostAvatar;
}

export function getHostAvatar() {
  return hostAvatar;
}

const TRUTHY = new Set([true, 1, "1", "true"]);

function boolish(v) {
  return TRUTHY.has(v);
}

/** 代下状态。规范字段 **显式给出即定案**（false 也定案，绝不被旧候选位的
 * 陈旧真值翻案）；`mode_options.delegation` 缺席时才按候选位回落：
 * 顶层 delegated → status → options → view → seats[me]。 */
export function delegatedOf(match) {
  if (!match || typeof match !== "object") return false;
  const mo = match.mode_options;
  if (mo && typeof mo === "object" && mo.delegation != null) {
    return boolish(mo.delegation);
  }
  if (boolish(match.delegated)) return true;
  if (boolish(match.status && match.status.delegated)) return true;
  if (boolish(match.options && match.options.delegated)) return true;
  if (boolish(match.view && match.view.delegated)) return true;
  const me = typeof match.me === "number" ? match.me : -1;
  const seat = Array.isArray(match.seats) ? match.seats[me] : null;
  return !!(seat && boolish(seat.delegated));
}

/** 终局判定（与 match.js 的 isOver 同语义；供无 DOM 的纯逻辑层使用）。 */
export function matchOver(match) {
  if (!match || typeof match !== "object") return false;
  const st = match.status || {};
  if (st.outcome != null && st.outcome !== "") return true;
  const v = match.view;
  if (!v) return false;
  // 终局信号按棋种取：五子棋 winLine/full；黑白棋 over（双方无子可落/吃光）；
  // 牌类 phase=over（打满局数）。不写死单一棋种，旧视图缺字段时安全降级为「未结束」。
  return !!(v.winLine || v.full || v.over || v.phase === "over");
}

/** 能否代下：宿主主 agent 是队友（陪练局 / C·D 阵容）+ 本人在座（人类座位）+ 未终局
 * （观战/宿主主 agent 参战局/终局一律不行）。 */
export function canDelegate(match) {
  if (!match || typeof match !== "object") return false;
  if (!isCoachMatch(match)) return false;
  if (typeof match.me !== "number" || match.me < 0) return false;
  const seats = Array.isArray(match.seats) ? match.seats : [];
  if (seats.length && seats[match.me] && seats[match.me].kind !== "human") return false;
  return !matchOver(match);
}

/* ---------------- 座位展示名（防「宿主主 agent 冒充对手」） ---------------- */

const SEAT_FALLBACK_NAMES = ["黑方", "白方"];

/** 座位展示名：服务端下发的真实名字优先，绝不再按模式强行改写；名字缺席时，
 * 陪练局进阶对手座位兜底「对手」、规则陪练座位兜底「陪练」，其余按棋色兜底。
 * 人设本人局（含 legacy ai 座位局）同样保持服务端名字。防冒充由头像归属保证：
 * 陪练局人设头像只进队友区，绝不挂对手位。 */
export function seatDisplay(seat, index, practice) {
  const name = seat && seat.name != null ? String(seat.name).trim() : "";
  if (name) return name;
  if (practice && seat && seat.kind === "bot") return "陪练";
  if (practice && seat && seat.kind === "ai") return "对手";
  return SEAT_FALLBACK_NAMES[index] || `座位 ${(index || 0) + 1}`;
}

/** 座位上的身份小片：人设本人局 ai 座位=人设名；陪练局进阶对手座位=「对手」、
 * 陪练座位=「陪练」（名字本身已展示，不写死任何人设名）。 */
export function seatKindLabel(seat, practice) {
  const kind = seat && seat.kind;
  // turn-driven ai seat = the host AI itself, not the plugin persona.
  if (kind === "ai") return practice ? "对手" : getHostAgentName();
  if (kind === "bot") return "陪练";
  return "";
}

/* ---------------- 状态行（代下分支） ---------------- */

/** 代下时的状态行文案：本方回合 = 代下中；对方回合 = 已开启等待接手。
 * 不可代下（非本人/未开启）返回 null，由调用方走原状态逻辑。
 * act 可选「代打」（牌类用词），默认「代下」保持棋类文案不变。 */
export function delegatedTurnStatus({ mine, delegated, myTurn, act } = {}) {
  if (!mine || !delegated) return null;
  const word = String(act || "") === "代打" ? "代打" : "代下";
  // Delegation is done by the host agent (it drives the notice turn).
  const who = getHostAgentName();
  return myTurn
    ? { kind: "delegated", text: `${who}${word}中…` }
    : { kind: "wait", text: `${word}已开启：轮到你时${who}接手` };
}

/* ---------------- 载荷构造 ---------------- */

/** 开局载荷（后端 room.create(mode_options=…) 规范形态）。mode 缺失（旧入口/
 * 旧后端）→ 旧契约 { kind, opponent }。practice 缺 difficulty → 默认 easy。
 * 三人桌阵容（lineup）：写入 lineup + 座位驱动派生值，2 座位调用方不会传。
 * level 只在显式给出时写入：nori / agent 局带三档，陪练局不带。 */
export function createMatchPayload(kind, { mode, difficulty, opponent, lineup, hands, level } = {}) {
  const plan = lineupPlanOf(lineup);
  if (plan) {
    const payload = {
      kind,
      mode_options: {
        lineup: plan.lineup,
        seat_drivers: [...plan.drivers],
        nori_seat: plan.noriSeat,
        nori_role: plan.noriRole,
        delegation: false,
      },
    };
    if (hands != null && String(hands).trim() !== "") payload.options = { hands: String(hands).trim() };
    return payload;
  }
  const lvl = normalizeLevel(level) || "intermediate";
  const withLevel = level != null && String(level).trim() !== "";
  const m = normalizeMode(mode);
  if (m === "nori") {
    return { kind, mode_options: { mode: "nori", ...(withLevel ? { level: lvl } : {}) } };
  }
  if (m === "practice") {
    const diff = normalizeDifficulty(difficulty) || "easy";
    // 三档只对「当前 agent 对手」（advanced 驱动 ai 座位）有意义；
    // 陪练局不写 level（后端也会忽略）。
    return {
      kind,
      mode_options: {
        mode: "bot",
        difficulty: diff,
        ...(withLevel && diff === "advanced" ? { level: lvl } : {}),
      },
    };
  }
  return { kind, opponent: opponent === "bot" ? "bot" : "ai" };
}

/** 代下开关动作体（client_version 由调用方按现有 act() 约定附加）。 */
export function delegateAction(enabled) {
  return { type: "delegate", delegated: !!enabled };
}

/** 重开/我也来一局的载荷：优先沿用当前对局的模式、难度与三档等级；mode 全缺时
 * 按座位构成推断（bot 座位局 → 陪练局规范载荷，与后端 legacy 回填一致；
 * 其余回落旧推断——对手座位 kinds：无 ai/bot 时按 bot，与原 deriveOpponent 同语义）。
 * 三人桌：阵容与局数（view.options.hands）一并沿用。 */
export function restartPayload(view) {
  const rawKind = String((view && view.kind) || "");
  const kind = /^[a-z][a-z0-9_]*$/.test(rawKind) ? rawKind : "gomoku";
  const lineup = lineupOf(view);
  if (lineup) {
    const hands = view && view.options && view.options.hands != null ? view.options.hands : undefined;
    return createMatchPayload(kind, { lineup, hands });
  }
  const { mode, difficulty } = modeOf(view);
  if (mode) {
    const level = levelVisible(view) ? levelOf(view) : "";
    return createMatchPayload(kind, { mode, difficulty, level });
  }
  const seats = Array.isArray(view && view.seats) ? view.seats : [];
  const kinds = seats.map((s) => (s && s.kind) || "");
  const legacy = kinds.includes("ai") ? "ai" : "bot";
  return createMatchPayload(kind, { opponent: legacy });
}

/** 重开按钮文案：观战者「我也来一局」；操盘者「重开一局」（终局「再来一局」）。 */
export function restartLabel({ spectator, over }) {
  if (spectator) return "我也来一局";
  return over ? "再来一局" : "重开一局";
}
