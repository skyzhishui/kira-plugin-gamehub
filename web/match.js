/* match.js — 对局页：座位条 / canvas 棋盘 / 动作行 / 状态行 / 走子日志。
 *
 * - token：URL ?t= 优先，其次 sessionStorage["gamehub_match_<id>"]；都没有即观战
 * - 连接：MatchConnection（先 GET 快照再连 WS，退避心跳，hidden 暂停）
 * - 状态以服务端 MatchView 为唯一真相：本地不做乐观落子；409 → 重拉快照 + toast
 * - 所有服务端文本一律 textContent 渲染
 * - 聊天：本插件没有宿主 webchat 面板（牌桌旁白走插件 notice 通道），页面只
 *   渲染 match 视图 / WS 帧里的气泡：人设座位气泡 + 陪练/进阶对手座位气泡
 *   （match.chat 增量，初载只记水位，历史不重播）
 * - 媒体：match.chat / 气泡里的图片与语音走宿主归档媒体端点（shared/media.js）
 */

import { api, ApiError, MatchConnection, readSessionToken, writeSessionToken, PAGE_BASE } from "./shared/api.js";
import { spectatorMode, enterSpectator } from "./shared/spectator.js";
import { errorText } from "./shared/i18n.js";
import { toast } from "./shared/toast.js";
import { confettiBurst } from "./shared/confetti.js";
import { loadGameModule } from "./games/registry.js";
import { createChatPanel } from "./chat-panel.js";
import {
  fetchUiConfig,
  isChatReadOnly,
  noticeTargetFromConfig,
  matchSayKey,
  pickNewMatchSays,
  CHAT_READ_ONLY_HINT,
} from "./shared/chat.js";
import {
  createBubbleQueue,
  BUBBLE_MIN_SHOW_MS,
  BUBBLE_MAX_SHOW_MS,
} from "./shared/bubble-queue.js";
import {
  isPractice,
  isCoachMatch,
  isThreeSeatMatch,
  noriSeatOf,
  noriRoleOf,
  seatIdentity,
  seatIdentityLabel,
  delegatedOf,
  canDelegate,
  delegatedTurnStatus,
  delegateAction,
  restartPayload,
  restartLabel,
  seatDisplay,
  seatKindLabel,
  levelOf,
  levelVisible,
  LEVEL_VALUES,
  LEVEL_LABELS,
  LEVEL_HINTS,
  MATE_NOTE_TEXT,
  setPersonaName,
  getPersonaName,
  setHostAgentName,
  getHostAgentName,
  setAgentAvatar,
  setHostAvatar,
  getAgentAvatar,
  getHostAvatar,
} from "./shared/mode.js";
import {
  moveText as doudizhuMoveText,
  parseCard as doudizhuParseCard,
  seatRoleLabel as doudizhuRoleLabel,
  seatRoleClass as doudizhuRoleClass,
} from "./games/doudizhu/logic.js";
import { cardChip as doudizhuCardChip } from "./games/doudizhu/table.js";
import "./shared/atmo.js";

const $ = (id) => document.getElementById(id);

const GAME_NAMES = { gomoku: "五子棋", reversi: "黑白棋", doudizhu: "斗地主", chess: "国际象棋" };
const LOG_SHOW = 12;

/* ---------------- 状态 ---------------- */

const params = new URLSearchParams(location.search);
const matchId = (params.get("id") || params.get("game") || "").trim();
const urlToken = params.get("t") || "";
let seatToken = spectatorMode() ? "" : urlToken || (matchId ? readSessionToken(matchId) : "");
if (urlToken && matchId) writeSessionToken(matchId, urlToken);

let conn = null;
let view = null; // 最新 MatchView（服务端唯一真相）
let busy = false; // 动作在途：禁用落子与按钮
let gameCtl = null; // 游戏模块控制器
let moduleReady = null;
let resignArmed = false;
let resignTimer = 0;
let chatWin = null; // 对局聊天窗口（chat-panel.js；座位气泡仍由下方 chat 对象负责）

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = String(text); // 一律 textContent，杜绝 HTML 注入
  return n;
}

function getToken() {
  return conn && conn.tokenRejected ? "" : seatToken;
}

function gameName(kind) {
  return GAME_NAMES[kind] || kind || "对局";
}

function shortId(id) {
  const s = String(id || "");
  // 末尾 6 位即可辨认；再剥掉可能落在开头的分隔符（对局 #-new-3 → #new-3）
  return (s.length > 6 ? s.slice(-6) : s).replace(/^[-_]+/, "");
}

/* ---------------- 派生状态 ---------------- */

function isOver(match) {
  if (!match) return false;
  const st = match.status || {};
  if (st.outcome != null && st.outcome !== "") return true;
  const v = match.view;
  // 终局信号按棋种取：五子棋 winLine/full；黑白棋 over（吃光/双停/满盘）；
  // 斗地主 phase=over（打满局数）。缺字段安全降级为「未结束」。
  return !!(v && (v.winLine || v.full || v.over || v.phase === "over"));
}

function canAct(match) {
  return !!(match && typeof match.me === "number" && match.me >= 0 && !isOver(match) && !busy);
}

function waitingOnSeat(match) {
  if (!match) return null;
  const st = match.status || {};
  let wo = typeof st.waitingOn === "number" ? st.waitingOn : null;
  // 后端 MatchView.status.waitingOn 是座位列表；单座位回合取唯一项
  if (wo == null && Array.isArray(st.waitingOn) && st.waitingOn.length === 1 && Number.isInteger(st.waitingOn[0])) {
    wo = st.waitingOn[0];
  }
  const v = match.view;
  if (wo == null && v && (+v.toPlay === 1 || +v.toPlay === 2)) wo = +v.toPlay - 1;
  // 三人桌（斗地主）以 view.turn 为当轮座位；不假定座位交替。
  // 终局 view.turn 为 null：+null===0 会伪装成座位 0，必须显式排除。
  if (wo == null && v && v.turn != null && Number.isInteger(+v.turn) && +v.turn >= 0 && (+v.turn >= 2 || isThreeSeatMatch(match))) {
    wo = +v.turn;
  }
  const seats = Array.isArray(match.seats) ? match.seats : [];
  return wo != null && seats[wo] ? { index: wo, seat: seats[wo] } : null;
}

function isCardGame(match) {
  return String((match && match.kind) || "") === "doudizhu";
}

/** 座位卡上是否显示「剩余张数」：牌类公开信息（counts）。 */
function seatCountOf(match, index) {
  if (!isCardGame(match)) return null;
  const counts = match.view && Array.isArray(match.view.counts) ? match.view.counts : [];
  const n = Number(counts[index]);
  return Number.isInteger(n) && n >= 0 ? n : null;
}

function turnVerb(match) {
  const v = match && match.view;
  if (isCardGame(match)) return String(v && v.phase) === "bid" ? "叫分" : "出牌";
  return "落子";
}

function statusInfo(match) {
  const st = match.status || {};
  if (isOver(match)) return { kind: "over", text: st.resultText || "对局结束" };
  const w = waitingOnSeat(match);
  const mine = typeof match.me === "number" && match.me >= 0;
  const practice = isPractice(match);
  // S3/S4 代下状态行：持久展示（开 → 本方回合「人设代下中…」/ 对方回合「等待接手」）
  const delegated = mine && delegatedOf(match);
  const delInfo = delegatedTurnStatus({
    mine,
    delegated,
    myTurn: !!(w && w.index === match.me),
    act: isCardGame(match) ? "代打" : "代下",
  });
  if (delInfo) return delInfo;
  if (w) {
    const { index, seat } = w;
    if (match.me === index) return { kind: "your-turn", text: `轮到你${turnVerb(match)}` };
    if (seat.kind === "human" && !seat.joined) return { kind: "wait", text: "等待对手入座…" };
    const identity = seatIdentity(match, seat, index);
    if (isCardGame(match)) {
      const verb = String(match.view && match.view.phase) === "bid" ? "叫分中…" : "出牌中…";
      if (identity === "nori") return { kind: "thinking", text: `${getHostAgentName()}${verb}` };
      return { kind: "thinking", text: `${seatDisplay(seat, index, practice)} ${verb}` };
    }
    if (identity === "nori") return { kind: "thinking", text: `${getHostAgentName()}思考中…` };
    return { kind: "thinking", text: `${seatDisplay(seat, index, practice)} 思考中…` };
  }
  return { kind: "wait", text: "等待开局…" };
}

/* ---------------- 时间格式 ---------------- */

function asDate(t) {
  if (t == null || t === "") return null;
  const d = new Date(t);
  return Number.isNaN(d.getTime()) ? null : d;
}

function fmtClock(t) {
  const d = asDate(t);
  if (!d) return "";
  const pad = (n) => String(n).padStart(2, "0");
  const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  const now = new Date();
  if (d.toDateString() === now.toDateString()) return hm;
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${hm}`;
}

/* ---------------- 渲染 ---------------- */

const CONN_TEXT = {
  loading: "连接中…",
  connecting: "连接中…",
  live: "实时同步",
  retrying: "重连中…",
  hidden: "已暂停",
  stopped: "插件已停用",
  missing: "对局不存在",
};

function renderConn(state, info = {}) {
  const chip = $("conn-chip");
  chip.className = `conn-chip ${state}`;
  $("conn-label").textContent = CONN_TEXT[state] || state;
  const banner = $("recon-banner");
  const show = state === "retrying";
  banner.classList.toggle("hidden", !show);
  if (show) {
    banner.textContent = `连接断开，正在重连${info.attempt ? `（第 ${info.attempt} 次）` : ""}…`;
  }
}

function applyView(match) {
  if (!match || !match.id) return;
  // 防回退：version 单调
  if (view && Number.isFinite(+match.version) && Number.isFinite(+view.version) && +match.version < +view.version) {
    return;
  }
  const first = !view;
  view = match;

  $("m-title").textContent = gameName(match.kind);
  $("m-sub").textContent = `对局 #${shortId(match.id)}`;
  const mine = typeof match.me === "number" && match.me >= 0;
  $("spectate-chip").classList.toggle("hidden", mine);
  $("spectate-chip").textContent = "观战中";

  renderSeats(match);
  renderMateZone(match);
  applyMatchChat(match, first);
  renderStatus(match);
  showResultSticker(match);
  maybeConfetti(match);
  renderLog(match);
  renderActions(match);

  if (first) document.title = `${gameName(match.kind)} · 游戏中心`;
  ensureModule(match).then(() => refreshBoard());
}

function refreshBoard() {
  if (!gameCtl || !view) return;
  // 代下中棋盘不归人手：人设接手本方回合，落子输入关闭（状态行有说明）
  const humanPlays = canAct(view) && !delegatedOf(view);
  gameCtl.update(view, { canAct: humanPlays, busy, over: isOver(view), delegated: delegatedOf(view) });
}

/* 座位小头像：加载失败退回名字首字，保留同尺寸槽位——卡片高度与气泡锚点
 * 不因图片 404 塌陷。公共 agent-avatar.png 自带 alpha，无需运行时色键
 * （shared/cleankey.js 保留给平涂底素材，本页不再需要）。
 * 已失败的 src → 重试期限（TTL Map）：座位卡随 WS 帧重建，若每次都再建
 * <img> 会重复发起 404 并闪一帧破图——期限内直接给占位，不再请求。
 * 错误事件无法区分 404 与网络抖动，故不永久定格：过期后下一帧自动重试
 * （插件缺席的真 404 每个 TTL 最多多一次请求，可忽略；弱网恢复后无需整页刷新）。 */
const AVATAR_FAIL_TTL_MS = 5 * 60 * 1000;
const failedAvatarSrcs = new Map();

function avatarKnownFailed(src) {
  const until = failedAvatarSrcs.get(src);
  if (until == null) return false;
  if (Date.now() >= until) {
    failedAvatarSrcs.delete(src);
    return false;
  }
  return true;
}

function seatAvatarFallback(name, thinking, agent) {
  const fb = el(
    "span",
    "seat-avatar avatar-fallback" + (agent ? " agent-avatar" : "") + (thinking ? " wiggle" : "")
  );
  fb.setAttribute("aria-hidden", "true");
  fb.textContent = Array.from(String(name || "?").trim())[0] || "?";
  return fb;
}

function seatAvatarEl(src, name, thinking, { agent = false } = {}) {
  if (avatarKnownFailed(src)) return seatAvatarFallback(name, thinking, agent);
  const av = document.createElement("img");
  av.className = "seat-avatar" + (agent ? " agent-avatar" : "") + (thinking ? " wiggle" : "");
  av.src = src;
  av.alt = "";
  av.addEventListener("error", () => {
    failedAvatarSrcs.set(src, Date.now() + AVATAR_FAIL_TTL_MS);
    if (!av.isConnected) return; // 已被重渲染换下：换新节点自己走占位分支
    av.replaceWith(seatAvatarFallback(name, thinking, agent));
  });
  return av;
}

/* 状态行 pill：文字包一层 .pill-text——320px 窄屏双 pill（如「人设代下」+
 * 「行棋中」）收缩时文字走省略号，而不是整枚被 .seat-chips 的 overflow
 * 裁掉。固定两行高度不变，完整状态仍在文字与语义里。 */
function chipsPill(cls, text) {
  const p = el("span", cls);
  p.appendChild(el("span", "pill-text", text));
  return p;
}

function renderSeats(match) {
  const box = $("seats");
  box.textContent = "";
  const seats = Array.isArray(match.seats) ? match.seats : [];
  const three = seats.length >= 3;
  box.classList.toggle("seats-3", three);
  const over = isOver(match);
  const w = waitingOnSeat(match);
  // 陪练局（人设是队友）：对手座位是陪练/进阶对手，绝不挂人设头像冒充对手；
  // 但对手自己的名字照常展示，仅名字缺席时兜底「陪练」。
  const practice = isPractice(match);
  // 三人桌（斗地主）：座位身份一律按 seat_drivers 判定，不按名字猜；
  // 人设参战局里人设本人座位按「对手位」语义处理（气泡/头像只挂他自己的座位）。
  const noriSeat = isThreeSeatMatch(match) ? noriSeatOf(match) : null;
  const noriSeatStyle = noriSeat != null && noriRoleOf(match) === "player";
  const seatPractice = (i) => practice || (noriSeatStyle && i !== noriSeat);
  const cardGame = isCardGame(match);

  seats.forEach((seat, i) => {
    const tile = el("div", "seat");
    if (!over && w && w.index === i) tile.classList.add("active");
    if (three) tile.classList.add("seat-compact");

    const identity = seatIdentity(match, seat, i);
    const name = seatDisplay(seat, i, seatPractice(i));
    const thinking = !over && !!(w && w.index === i);
    const isMine = typeof match.me === "number" && match.me === i;
    tile.dataset.seat = String(i);
    tile.dataset.identity = identity;

    // 头像口径：
    // - game_agent 座位（进阶对手/教练席）→ 插件自有 agent-avatar（默认 nori，可上传替换）；
    // - 宿主主 agent 座位 → 当前人设的自拍参考图（宿主下发 host_avatar）；
    //   宿主没配参考图时用名字首字占位，绝不借用 game_agent 的头像；
    // - 陪练/人类座位 → 名字首字占位，不冒充任何人设。
    let avatar = null;
    if (identity === "agent") {
      avatar = seatAvatarEl(getAgentAvatar(), name, thinking, { agent: true });
    } else if (identity === "nori" && getHostAvatar()) {
      avatar = seatAvatarEl(getHostAvatar(), name, thinking, { agent: true });
    } else if (three && (identity === "human" || identity === "bot" || identity === "nori")) {
      avatar = seatAvatarFallback(name, thinking, false);
    }

    // 三人桌：头像在左并带「剩余张数」角标（斗地主的公开信息），
    // 头像/身份/思考/气泡仍按座位一比一归属，绝不串台。
    if (three) {
      const wrap = el("span", "seat-av-wrap");
      if (avatar) wrap.appendChild(avatar);
      const count = seatCountOf(match, i);
      if (count != null) {
        const badge = el("span", "dz-count-badge", String(count));
        badge.title = `还剩 ${count} 张牌`;
        badge.setAttribute("aria-label", `还剩 ${count} 张牌`);
        wrap.appendChild(badge);
      }
      tile.appendChild(wrap);
    } else {
      tile.appendChild(el("span", `stone-ic s${i + 1}`));
    }

    const main = el("div", "seat-main");
    // 身份行：名字单行省略（完整名字进 title），身份小片紧随其后；
    // 状态行（思考中/代下中/等待加入）单独一行且高度固定——思考态切换不撑高卡片。
    const idRow = el("div", "seat-name-row");
    const nameEl = el("span", "seat-name", name);
    nameEl.title = name;
    idRow.appendChild(nameEl);
    if (isMine) idRow.appendChild(el("span", "pill you", "你"));
    // 三人桌：身份由名字承担（人设名/进阶对手名/陪练名），不再叠「陪练」小片；
    // 阵营片移到状态行，名字获得完整一行（375px 不挤成「阿…」）。
    const kindLabel = three
      ? cardGame ? "" : seatIdentityLabel(match, seat, i)
      : seatKindLabel(seat, seatPractice(i));
    if (kindLabel && kindLabel !== name) idRow.appendChild(el("span", "pill", kindLabel));
    main.appendChild(idRow);

    const chips = el("div", "seat-chips");
    if (cardGame && three) {
      // 窄屏隐藏头像后，张数改由状态行左侧的紧凑数字承担（桌面端用头像角标，此处 CSS 隐藏）
      const count = seatCountOf(match, i);
      if (count != null) chips.appendChild(el("span", "dz-seat-count", `${count} 张`));
    }
    // 自家回合是「轮到你」而不是思考中：阵营片照常显示；别家思考中让位给状态
    if (cardGame && three && !(thinking && !isMine)) {
      const role = doudizhuRoleLabel(match.view, i);
      if (role) chips.appendChild(el("span", `pill dz-role ${doudizhuRoleClass(match.view, i)}`, role));
    }
    if (seat.kind === "human" && !seat.joined) chips.appendChild(chipsPill("pill warn", "等待加入"));
    // S3 代下 pill：所有视角可见（观战也知道这手由人设代下），只读不可切换
    if (!over && delegatedOf(match) && seat.kind === "human") {
      const who = getHostAgentName();
      chips.appendChild(chipsPill("pill accent delegate-pill", isCardGame(match) ? `${who}代打` : `${who}代下`));
    }
    if (thinking && !isMine) {
      if (seat.kind === "ai" || seat.kind === "bot") {
        const tp = el("span", "pill think");
        const who = (three ? seatIdentityLabel(match, seat, i) : seatKindLabel(seat, seatPractice(i))) || "思考中";
        const verb = cardGame ? (String(match.view && match.view.phase) === "bid" ? "叫分中" : "出牌中") : "思考中";
        tp.setAttribute("aria-label", `${who}${verb}`);
        tp.appendChild(document.createTextNode(verb));
        if (!cardGame) for (let k = 0; k < 3; k++) tp.appendChild(el("i", "tdot"));
        chips.appendChild(tp);
      } else if (!(seat.kind === "human" && !seat.joined)) {
        chips.appendChild(chipsPill("pill", "行棋中"));
      }
    }
    main.appendChild(chips);
    tile.appendChild(main);

    // 人设/进阶对手座位都挂公共头像 + 各自的 say 气泡（per-seat 单例，
    // 座位每次重渲染时重新挂载，不重建、不打断计时）。
    if (!three && identity === "nori") {
      tile.appendChild(
        getHostAvatar()
          ? seatAvatarEl(getHostAvatar(), name, thinking, { agent: true })
          : seatAvatarFallback(name, thinking, false)
      );
      const bubble = seatBubbleEl(i);
      bubble.classList.remove("bubble-top");
      tile.appendChild(bubble);
    } else if (!three && identity === "agent") {
      tile.appendChild(seatAvatarEl(getAgentAvatar(), name, thinking, { agent: true }));
      tile.appendChild(seatBubbleEl(i));
    } else if (!three && practice && seat.kind !== "human") {
      // 陪练局：对手座位 say 气泡挂对手座位（独立单例）。
      tile.appendChild(seatBubbleEl(i));
    }

    // 三人桌：人设/对手 say 气泡都钉在各自座位卡上（per-seat 单例）。
    if (three && seat.kind !== "human") {
      const bubble = seatBubbleEl(i);
      bubble.classList.add("bubble-top");
      tile.appendChild(bubble);
    }

    box.appendChild(tile);
  });

  // 2 座位旧局保持原虚位补位；三人桌座位由服务端下发，不补。
  if (!three) {
    for (let i = seats.length; i < 2; i++) {
      box.appendChild(el("div", "seat ghost-seat", i === 0 ? "黑方虚位以待" : "白方虚位以待"));
    }
  }
}

function renderStatus(match) {
  const line = $("status-line");
  const info = statusInfo(match);
  line.className = `status-line k-${info.kind}`;
  const participant = typeof match.me === "number" && match.me >= 0;
  const spectating = !participant;
  line.textContent = spectating && !isOver(match) ? `观战中 · ${info.text}` : info.text;
  // 终局时结果已由玻璃胶囊（贴纸+结果词）呈现，状态行不再重复同一句
  const rt = String((match.status && match.status.resultText) || "");
  const draw = rt.includes("和") || rt.includes("平");
  line.classList.toggle("hidden", isOver(match) && participant && !draw);
}

/* S3 队友区：陪练局里人设是我方队友——头像/身份/代下状态/发言气泡都在这条
 * 专属区域，绝不占用对手座位。人设参战局（对手位）不渲染本区域。
 * 元素常驻（match.html），每次 applyView 重绘内容；观战同样可见（只读）。
 * 人设不占座（教练阵容）时没有座位号：气泡挂在队友区头像旁，按「教练座位」
 * （-1）的单例队列展示。 */
const COACH_SEAT = -1;

/** 队列键 = 发言归属座位。代下宿主发言（by="host"）归教练位气泡（队友区的
 * 宿主头像）；其余一律按发言者真实座位归各自的座位气泡——含 2 座位陪练局的
 * 对手座位（noriSeatOf 为 null 只代表宿主不占座，不代表所有发言都归教练位）。
 * 座位号缺失/越界（理论不应出现）回落教练位；教练位气泡未挂载时
 * showSeatSays 静默丢弃，绝不把发言冒充到人类座位上。 */
function talkSeatKey(match, entry) {
  if (entry && entry.by === "host") return COACH_SEAT;
  const idx = Number(entry && entry.seat);
  const seats = Array.isArray(match && match.seats) ? match.seats : [];
  if (Number.isInteger(idx) && idx >= 0 && idx < seats.length) return idx;
  return COACH_SEAT;
}

function renderMateZone(match) {
  const zone = $("mate-zone");
  if (!zone) return;
  const show = !!match && isCoachMatch(match);
  zone.classList.toggle("hidden", !show);
  if (!show) {
    const old = chat.seatBubbles.get(COACH_SEAT);
    if (old && old.parentElement === zone) old.classList.remove("bubble-top");
    return;
  }
  zone.textContent = "";

  const over = isOver(match);
  const mine = typeof match.me === "number" && match.me >= 0;
  const delegated = delegatedOf(match);
  const w = waitingOnSeat(match);
  const noriActing = delegated && !over && mine && !!(w && w.index === match.me);
  // The teammate/coach seat is the host agent (it does the delegating).
  const persona = getHostAgentName();

  // 教练/队友席是宿主主 agent：优先用当前人设的自拍参考图；宿主没配时
  // 用名字首字占位。绝不套 game_agent 的头像。
  const avWrap = el("span", "mate-avatar-wrap");
  const hostSrc = getHostAvatar();
  let av;
  if (hostSrc) {
    av = seatAvatarEl(hostSrc, persona, noriActing, { agent: true });
    av.className = "mate-avatar" + (noriActing ? " wiggle" : "");
  } else {
    av = seatAvatarFallback(persona, noriActing, false);
    av.className = "mate-avatar mate-avatar-fallback" + (noriActing ? " wiggle" : "");
  }
  avWrap.appendChild(av);
  // 人设发言气泡挂头像包裹层内（侧向气泡，尾巴指向头像）。只挂一次：
  // 之前的重复 append 会把气泡移到 zone，导致脱离头像锚点。
  const bubble = seatBubbleEl(COACH_SEAT);
  bubble.classList.remove("bubble-top");
  avWrap.appendChild(bubble);
  zone.appendChild(avWrap);

  const nameBox = el("span", "mate-name-box");
  nameBox.appendChild(el("span", "mate-name", persona));
  nameBox.appendChild(el("span", "pill accent", "队友"));
  zone.appendChild(nameBox);

  const word = isCardGame(match) ? "代打" : "代下";
  if (!over && delegated) zone.appendChild(el("span", "pill accent mate-pill", `${word}中`));
  else if (!over && canDelegate(match)) zone.appendChild(el("span", "pill mate-standby", `可${word}`));

  // 说明文字单独成块（宽屏与身份/状态同排；窄屏按剩余宽度省略），
  // 绝不把身份行挤换行，队友区高度恒定。
  zone.appendChild(
    el("span", "mate-text", isCardGame(match) ? "聊天里给你支招，也能替你出牌" : MATE_NOTE_TEXT)
  );
}

/* 结果层：半透明毛玻璃胶囊（贴纸 + 结果词）。
 * 参局者终局时出现：胜=sticker-win 从下弹出（overshoot），负=sticker-lose 轻微摇头；
 * 和棋保持克制只改状态行；观战不出结果层；图片缺失自动消失；pointer-events:none 不挡棋盘。 */
function outcomeWinners(match) {
  const o = match && match.status && match.status.outcome;
  const winners = o && typeof o === "object" ? o.winners : null;
  return Array.isArray(winners) ? winners.filter((i) => Number.isInteger(i)) : [];
}

function showResultSticker(match) {
  if (!match) return;
  const slot = $("sticker-slot");
  if (isCardGame(match)) {
    // 牌类终局由牌桌自己的结算面板呈现（分数/春天/炸弹），不叠贴纸。
    if (slot) slot.textContent = "";
    return;
  }
  if (!slot) return;
  slot.textContent = "";
  if (!isOver(match) || typeof match.me !== "number" || match.me < 0) return;
  const rt = String((match.status && match.status.resultText) || "");
  if (rt.includes("和") || rt.includes("平")) return;
  // 胜方以 status.outcome.winners 为准（各棋种座位语义一致）；缺字段时回落
  // 结果文案里的「你执的一方 + 胜」判断，兼容旧视图。
  const winners = outcomeWinners(match);
  const mine = match.me === 0 ? "黑方" : "白方";
  const won = winners.length ? winners.includes(match.me) : rt.includes(mine) && rt.includes("胜");
  const glass = el("div", "result-glass");
  const img = document.createElement("img");
  img.className = `result-sticker ${won ? "win" : "lose"}`;
  // 胜负贴纸（通用素材，不含角色插画）；缺失自动移除，结果文字兜底。
  img.src = won ? "./assets/sticker-win.png" : "./assets/sticker-lose.png";
  img.alt = won ? "这局你赢了" : "这局输了";
  img.addEventListener("error", () => img.remove());
  glass.appendChild(img);
  glass.appendChild(el("span", "result-text", rt || (won ? "你赢了！" : "这局输了")));
  slot.appendChild(glass);
}

/* 胜利彩纸：进入终局（有胜方）时在棋桌上方爆一次（≤80 片）。
 * 以 matchId+version 为去重键，同一局面不重复放。 */
let confettiKey = "";
function maybeConfetti(match) {
  if (!match || !isOver(match)) return;
  // 牌类终局由牌桌结算面板呈现；不撒彩纸、不叠贴纸（结算里已有胜负/分数）
  if (isCardGame(match)) return;
  const rt = String((match.status && match.status.resultText) || "");
  const win = outcomeWinners(match).length > 0 || (match.view && match.view.winLine) || rt.includes("胜");
  if (!win) return;
  const key = `${match.id}:${match.version}`;
  if (key === confettiKey) return;
  confettiKey = key;
  confettiBurst($("board-shell"));
}

function renderLog(match) {
  const list = $("log-list");
  list.textContent = "";
  const log = Array.isArray(match.log) ? match.log : [];
  const recent = log.slice(-LOG_SHOW).reverse();
  if (!recent.length) {
    list.appendChild(
      el("li", "log-empty", isCardGame(match) ? "还没有出牌，牌桌等第一手。" : "还没有落子，棋盘等第一手。")
    );
    return;
  }
  const seats = Array.isArray(match.seats) ? match.seats : [];
  const practice = isPractice(match);
  recent.forEach((entry, idx) => {
    const li = el("li", "log-row" + (idx === 0 ? " latest" : ""));
    const si = typeof entry.seat === "number" ? entry.seat : null;
    const seat = si != null ? seats[si] : null;
    if (isCardGame(match)) li.appendChild(el("span", "dz-log-ic"));
    else li.appendChild(el("span", `stone-ic sm s${si != null ? si + 1 : 1}`));
    li.appendChild(
      el(
        "span",
        "log-who",
        seat
          ? seatDisplay(seat, si, practice)
          : si != null
            ? si === 0
              ? "黑方"
              : "白方"
            : "—"
      )
    );
    const rawMove = entry.move != null ? String(entry.move) : "?";
    // 牌类：出牌记录也渲染真实牌面（花牌/王只显示点数花色角标），叫分/过牌/认输仍用文字。
    // 服务端 log.cards 优先；旧记录/测试 fixture 若只有牌 ID 文本则就地解析。
    const rawCards = Array.isArray(entry.cards) ? entry.cards.filter(Boolean) : [];
    const tokens =
      rawCards.length > 0
        ? rawCards
        : isCardGame(match) && rawMove !== "resign"
          ? rawMove.split(/[\s,，、]+/).filter(Boolean)
          : [];
    const parsed = tokens.map(doudizhuParseCard);
    if (parsed.length && parsed.every(Boolean)) {
      const row = el("span", "dz-log-cards");
      tokens.forEach((token) => row.appendChild(doudizhuCardChip(token)));
      li.appendChild(row);
    } else {
      const shown =
        isCardGame(match) && rawMove !== "resign"
          ? doudizhuMoveText(rawMove)
          : rawMove === "resign"
            ? "认输"
            : rawMove;
      li.appendChild(el("span", "mv", shown || "?"));
    }
    const t = fmtClock(entry.t);
    if (t) li.appendChild(el("time", "log-t", t));
    list.appendChild(li);
  });
}

function actionBtn({ label, cls = "", disabled = false, onClick }) {
  const b = el("button", `btn${cls ? ` ${cls}` : ""}`, label);
  b.type = "button";
  b.disabled = disabled;
  if (onClick) b.addEventListener("click", onClick);
  return b;
}

/** 三档等级（初级/中级/高级）：对人设本人 / 进阶对手显示；陪练没有
 * 威胁分析，不摆三档。服务端 POST /matches/{id}/mode 是唯一真相，
 * 失败不乐观更新（按钮态由下一次服务端视图纠正）。 */
const LEVEL_KINDS = new Set(["gomoku", "reversi"]);

function renderLevelRow(match) {
  const row = $("level-row");
  if (!row) return;
  row.textContent = "";
  const mine = typeof match.me === "number" && match.me >= 0;
  const show = !!mine && !isOver(match) && LEVEL_KINDS.has(String(match.kind || "")) && levelVisible(match);
  row.classList.toggle("hidden", !show);
  if (!show) return;
  const current = levelOf(match);
  row.appendChild(el("span", "level-label", "难度"));
  for (const lv of LEVEL_VALUES) {
    const b = el("button", `level-chip${lv === current ? " on" : ""}`, LEVEL_LABELS[lv]);
    b.type = "button";
    b.dataset.level = lv;
    b.title = LEVEL_HINTS[lv];
    b.setAttribute("aria-pressed", String(lv === current));
    b.disabled = busy;
    b.addEventListener("click", () => onLevelChange(lv));
    row.appendChild(b);
  }
}

async function onLevelChange(level) {
  if (!view || busy) return;
  if (!LEVEL_VALUES.includes(level) || levelOf(view) === level) return;
  if (!seatToken || (conn && conn.tokenRejected)) return;
  busy = true;
  renderActions(view);
  try {
    // 只改等级时 mode/difficulty 留空：服务端沿用当前值（不会把 agent 局降级成陪练）。
    const data = await api(
      `/matches/${encodeURIComponent(matchId)}/mode?t=${encodeURIComponent(seatToken)}`,
      { method: "POST", body: { level } }
    );
    const merged = data && data.mode_options && typeof data.mode_options === "object" ? data.mode_options : null;
    if (merged) view = { ...view, mode_options: { ...(view.mode_options || {}), ...merged } };
    toast(`难度已改为${LEVEL_LABELS[level]}`, "ok");
  } catch (err) {
    toast(errorText(err, "难度切换失败，请重试"), "error");
  } finally {
    busy = false;
    if (view) {
      renderActions(view);
      refreshBoard();
    }
  }
}

function renderActions(match) {
  const row = $("actions");
  row.textContent = "";
  if (!match) return;
  renderLevelRow(match);
  const over = isOver(match);
  const mine = typeof match.me === "number" && match.me >= 0;

  // S4 重开按钮：操盘者「重开一局」（终局「再来一局」）；观战者「我也来一局」
  if (mine) row.appendChild(actionBtn({
    label: restartLabel({ spectator: !mine, over }),
    cls: mine && !over ? "" : "primary",
    disabled: busy,
    onClick: newMatch,
  }));

  // S3 代下开关：仅陪练局/教练阵容 + 本人在座（观战者不渲染，后端同样只认座位口令）
  if (mine && canDelegate(match)) {
    const delegated = delegatedOf(match);
    const word = isCardGame(match) ? "代打" : "代下";
    row.appendChild(actionBtn({
      label: delegated ? `停止${word}` : `让${getHostAgentName()}${word}`,
      cls: delegated ? "delegate on" : "delegate",
      disabled: busy,
      onClick: onToggleDelegate,
    }));
  }

  if (mine && !over) {
    row.appendChild(actionBtn({
      label: resignArmed ? "再点一次确认认输" : "认输",
      cls: resignArmed ? "danger" : "",
      disabled: busy,
      onClick: onResign,
    }));
  }

  if (spectatorMode()) return;
  if (!over) row.appendChild(actionBtn({ label: "复制观战链接", onClick: copyLink }));

  const back = el("a", "btn ghost", "返回大厅");
  back.href = "./index.html";
  row.appendChild(back);
}

/* ---------------- 动作 ---------------- */

async function act(body) {
  if (!view || busy) return false;
  if (!seatToken || (conn && conn.tokenRejected)) {
    toast("观战视角不能执行这个操作", "error");
    return false;
  }
  busy = true;
  renderActions(view);
  refreshBoard();
  try {
    const payload = { ...body };
    if (Number.isFinite(+view.version)) payload.client_version = +view.version;
    const data = await api(
      `/matches/${encodeURIComponent(matchId)}/actions?t=${encodeURIComponent(seatToken)}`,
      { method: "POST", body: payload }
    );
    if (data && data.match) applyView(data.match);
    return true;
  } catch (err) {
    if (err instanceof ApiError && err.status === 409) {
      toast("版本已更新，已同步最新棋局", "info");
      await resnapshot();
    } else if (err instanceof ApiError && (err.status === 401 || err.status === 403)) {
      toast(errorText(err, "没有权限执行这个操作"), "error");
      await resnapshot();
    } else {
      toast(errorText(err, "操作失败，请重试"), "error");
    }
    return false;
  } finally {
    busy = false;
    if (view) {
      renderActions(view);
      refreshBoard();
    }
  }
}

async function resnapshot() {
  if (!conn) return;
  try {
    await conn.pullSnapshot();
  } catch (err) {
    toast(errorText(err, "刷新棋局失败"), "error");
  }
}

function place(p) {
  if (busy || !view || !canAct(view)) return;
  act({ type: "move", move: p.move });
}

function onResign() {
  if (!resignArmed) {
    resignArmed = true;
    renderActions(view);
    toast("再点一次确认认输", "info");
    resignTimer = setTimeout(() => {
      resignArmed = false;
      if (view) renderActions(view);
    }, 4000);
    return;
  }
  clearTimeout(resignTimer);
  resignTimer = 0;
  resignArmed = false;
  act({ type: "resign" });
}

/* S3 代下开关（让人设代下 ⇄ 停止代下）。服务端为唯一真相：
 * - 开：当前已是本方回合 → 立刻触发人设落这一手；否则挂起，轮到本方自动接手（持久）。
 * - 关：人设已被授予的这一手可以下完，下一手起不再代下。
 * 成败以响应为准（失败 act() 已 toast，UI 不谎报状态）。 */
async function onToggleDelegate() {
  if (!view || busy) return;
  const next = !delegatedOf(view);
  const ok = await act(delegateAction(next));
  if (!ok) return;
  const word = isCardGame(view) ? "代打" : "代下";
  const who = getHostAgentName();
  if (next) {
    const w = waitingOnSeat(view);
    if (w && w.index === view.me) toast(`已开启${word}，${who}正在替你走这一手`, "ok");
    else toast(`已开启${word}，轮到你时${who}接手`, "ok");
  } else {
    toast(`已停止${word}：${who}若正在走这手会走完，下一手起你自己来`, "info");
  }
}

async function newMatch() {
  if (!view || busy) return;
  const over = isOver(view);
  const mine = typeof view.me === "number" && view.me >= 0;
  // 单 actor 单局：未结束就重开必须先认输，否则服务端会返回现有局（看起来像没反应）
  if (!over && mine) {
    if (!window.confirm("当前对局还没结束，重开将判为认输。确定吗？")) return;
  }
  busy = true;
  renderActions(view);
  try {
    if (!over && mine && seatToken && !(conn && conn.tokenRejected)) {
      const payload = { type: "resign" };
      if (Number.isFinite(+view.version)) payload.client_version = +view.version;
      await api(
        `/matches/${encodeURIComponent(matchId)}/actions?t=${encodeURIComponent(seatToken)}`,
        { method: "POST", body: payload }
      );
    }
    // S4：重开沿用当前局的模式/难度（陪练局连难度一起带上）；旧对局回落旧推断
    const data = await api("/matches", { method: "POST", body: restartPayload(view) });
    const m = data && data.match;
    if (!m || !m.id) throw new ApiError("开局响应缺少对局信息", 0);
    writeSessionToken(m.id, data.token || "");
    location.href = `./match.html?id=${encodeURIComponent(m.id)}`;
  } catch (err) {
    toast(errorText(err, "开局失败，请稍后再试"), "error");
  } finally {
    busy = false;
    if (view) renderActions(view);
  }
}

async function copyLink() {
  let url;
  try {
    const invitation = await api(`/matches/${encodeURIComponent(matchId)}/invite`, { method: "POST", body: { origin: location.origin } });
    url = invitation.url;
  } catch (err) {
    toast(errorText(err, "无法创建观战邀请 / Cannot create invitation"), "error");
    return;
  }
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(url);
      toast("已复制观战链接", "ok");
      return;
    }
    throw new Error("clipboard unavailable");
  } catch {
    if (legacyCopy(url)) toast("已复制观战链接", "ok");
    else toast("复制失败，请手动复制地址栏链接", "error");
  }
}

function legacyCopy(text) {
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.setAttribute("readonly", "");
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  let ok = false;
  try {
    ok = document.execCommand("copy");
  } catch {
    ok = false;
  }
  ta.remove();
  return ok;
}


/* ---------------- 游戏模块 ---------------- */

function ensureModule(match) {
  if (moduleReady) return moduleReady;
  const shell = $("board-shell");
  // 棋种专属信息条槽位（黑白棋计数/pass；五子棋保持 hidden，不加高页面）
  const meta = $("board-meta");
  if (meta) {
    meta.textContent = "";
    meta.className = "board-meta hidden";
  }
  const kind = /^[a-z][a-z0-9_]*$/.test(String(match.kind || "")) ? match.kind : "gomoku";
  moduleReady = loadGameModule(kind)
    .then((mod) => {
      const skel = $("board-skel");
      if (skel) skel.remove();
      if (mod && mod.default && typeof mod.default.mount === "function") {
        gameCtl = mod.default.mount(shell, { onPlace: place, meta });
      } else {
        shell.appendChild(el("p", "board-missing", "该游戏的棋盘组件尚未实现，观战与记录不受影响。"));
      }
    })
    .catch(() => {
      const skel = $("board-skel");
      if (skel) skel.remove();
      shell.appendChild(el("p", "board-missing", "棋盘加载失败，请刷新重试。"));
    });
  return moduleReady;
}

/* ---------------- 座位发言气泡（match.chat） ----------------
 * 后端的牌桌发言只有一条真相链：match.chat = [{seat,text,t,at,by}]。人设本人
 * 的 say（行动回合工具）、代下宿主的 say（by="host"）与陪练/进阶对手的 say
 * 都以各自座位号落在这里，所以前端按「发言归属」维护独立单例气泡 + 独立排队器：
 * - 首帧（本局第一条快照/WS 帧）只播种水位，历史旧发言绝不自动弹；
 * - 之后仅比水位新的条目按到达顺序逐条展示：非人类座位弹在其座位卡上
 *   （人类手打 say 不弹）；代下宿主发言（by="host"）弹在队友区的宿主头像上；
 * - 每条至少 1s、最多 3s 收起（ui-config bubble_hold_s 只能在该区间内收紧）。
 * 本插件没有宿主 webchat 面板（旁白走插件 notice 通道），因此这里不再有
 * SSE / 历史拉取 / 发送入口，只保留只读渲染。
 */

const chat = {
  holdMs: 3000,
  minShowMs: 1000,
  seatBubbles: new Map(), // 座位号 → 气泡元素（独立于其它座位）
  seatQueues: new Map(), // 座位号 → 排队器
  saySeen: null, // match.chat 已见签名集合：首帧只播种不弹，增量才弹
  sayMatchId: "",
  readOnly: false, // chat_read_only=true（目标为非 web 会话）：只读提示
  statusError: "", // notice_target_error：目标诊断提示
};

function seatBubbleEl(seatIndex) {
  const idx = Number.isFinite(+seatIndex) ? +seatIndex : 0;
  if (!chat.seatBubbles.has(idx)) {
    const b = el("span", "seat-bubble");
    b.setAttribute("aria-hidden", "true"); // 文本不进读屏重复播报
    b.dataset.seat = String(idx);
    b.appendChild(el("span", "seat-bubble-media"));
    b.appendChild(el("span", "seat-bubble-text"));
    chat.seatBubbles.set(idx, b);
  }
  return chat.seatBubbles.get(idx);
}

function renderSeatBubble(seatIndex, item) {
  const idx = Number.isFinite(+seatIndex) ? +seatIndex : 0;
  const b = chat.seatBubbles.get(idx);
  if (!b || !item) return;
  const text = String(item.text == null ? "" : item.text).trim();
  if (!text) return;
  const textEl = b.querySelector(".seat-bubble-text") || b.lastElementChild;
  if (textEl) textEl.textContent = text;
  const mediaBox = b.firstElementChild;
  if (mediaBox && mediaBox.classList && mediaBox.classList.contains("seat-bubble-media")) mediaBox.textContent = "";
  b.classList.add("show");
}

function hideSeatBubbleAt(seatIndex) {
  const b = chat.seatBubbles.get(+seatIndex);
  if (!b) return;
  b.classList.remove("show");
  const textEl = b.querySelector(".seat-bubble-text") || b.lastElementChild;
  if (textEl) textEl.textContent = "";
}

function seatQueue(seatIndex) {
  const idx = Number.isFinite(+seatIndex) ? +seatIndex : 0;
  if (!chat.seatQueues.has(idx)) {
    chat.seatQueues.set(
      idx,
      createBubbleQueue({
        minShowMs: chat.minShowMs,
        maxShowMs: chat.holdMs,
        onShow: (item) => renderSeatBubble(idx, item),
        onUpdate: (item) => renderSeatBubble(idx, item),
        onHide: () => hideSeatBubbleAt(idx),
      })
    );
  }
  return chat.seatQueues.get(idx);
}

/** 队列化展示一条座位 say（按到达顺序，不合并、不覆盖）。 */
function showSeatSays(seatIndex, text, key) {
  const t = String(text == null ? "" : text).trim();
  const idx = Number.isFinite(+seatIndex) ? +seatIndex : 0;
  if (!t || !chat.seatBubbles.has(idx)) return;
  seatQueue(idx).enqueue({ key, text: t });
}

/** 复位全部座位气泡（换局/离页）：销毁排队器、清计时与排队、收起气泡。 */
function hideAllSeatBubbles() {
  chat.seatQueues.forEach((q) => q.dispose());
  chat.seatQueues.clear();
  chat.seatBubbles.forEach((b, idx) => hideSeatBubbleAt(idx));
}

/** 该条 say 是否需要气泡：代下宿主发言（后端 by="host"，按人类座位落库）
 * 始终展示（挂教练位气泡，见 talkSeatKey）；其余仅非人类座位且非本人视角
 * （人设本人 + 陪练/进阶对手都算）。 */
function isTalkSeat(match, seatIndex, seat, entry) {
  if (entry && entry.by === "host") return true;
  const seats = Array.isArray(match && match.seats) ? match.seats : [];
  const s = seat || seats[seatIndex];
  if (!s || s.kind === "human") return false;
  return !(typeof match.me === "number" && match.me === seatIndex);
}

/* WS 快照/帧 → 座位 say 增量。first=true（本局首帧）只记水位，历史旧发言
 * 绝不自动弹；之后仅比水位新的条目进入 pickNewMatchSays，并按到达顺序逐条
 * 入各自归属的队列。 */
function applyMatchChat(match, first) {
  if (!match) return;
  const arr = Array.isArray(match.chat) ? match.chat : [];
  const matchKey = String(match.id || "");
  if (first || chat.sayMatchId !== matchKey) {
    chat.sayMatchId = matchKey;
    chat.saySeen = new Set(arr.map(matchSayKey).filter(Boolean));
    hideAllSeatBubbles();
    return;
  }
  const { nextSeen, entries } = pickNewMatchSays(arr, chat.saySeen, {
    practice: true,
    seats: match.seats,
    me: match.me,
    allowSeat: (seatIndex, seat, entry) => isTalkSeat(match, seatIndex, seat, entry),
  });
  chat.saySeen = nextSeen;
  entries.forEach((e) => showSeatSays(talkSeatKey(match, e), e.text, matchSayKey(e)));
}

/* 只读/诊断提示：ui-config 的 chat_read_only 与 notice_target_error。
 * 对局页没有输入框，提示条落在棋盘与动作行之间（元素缺席时静默）。 */
function applyChatNotice() {
  const note = $("chat-readonly");
  if (note) {
    note.textContent = chat.readOnly ? CHAT_READ_ONLY_HINT : "";
    note.classList.toggle("hidden", !chat.readOnly);
  }
  const status = $("chat-status");
  if (status) {
    const show = !chat.readOnly && !!chat.statusError;
    status.textContent = show ? chat.statusError : "";
    status.classList.toggle("hidden", !show);
  }
}

/** 按局 ui-config：气泡上限 + 只读/诊断 + 人设名（全部可缺省降级）。 */
async function initUiConfig() {
  let cfg;
  try {
    cfg = await fetchUiConfig(matchId);
  } catch (err) {
    console.warn("ui-config 拉取失败（按默认值继续）", err);
    return;
  }
  if (!cfg || typeof cfg !== "object") return;
  if (cfg.agent_name) setPersonaName(cfg.agent_name);
  // mode "nori" 的座位是宿主 AI 本人（行动通知回合驱动），不是插件人设。
  if (cfg.host_agent_name) setHostAgentName(cfg.host_agent_name);
  // 头像：插件自有 agent-avatar（默认素材或 WebUI 上传版，脏值回落默认）；
  // 宿主主 agent 用当前人设的自拍参考图（空串 = 宿主没配，走首字占位）
  setAgentAvatar(cfg.agent_avatar);
  setHostAvatar(cfg.host_avatar);
  const holdFromCfg = Math.round(Number(cfg.bubble_hold_s) * 1000);
  chat.holdMs = Math.min(
    BUBBLE_MAX_SHOW_MS,
    Math.max(BUBBLE_MIN_SHOW_MS, Number.isFinite(holdFromCfg) && holdFromCfg > 0 ? holdFromCfg : BUBBLE_MAX_SHOW_MS)
  );
  chat.minShowMs = BUBBLE_MIN_SHOW_MS;
  chat.seatQueues.forEach((q) => q.setMaxShowMs(chat.holdMs));
  chat.readOnly = isChatReadOnly(cfg);
  chat.statusError = noticeTargetFromConfig(cfg).error;
  applyChatNotice();
  // 对局聊天窗口：聊天开关（chat_enabled）与宿主主 agent 名（agent 行 sender）
  if (chatWin) chatWin.applyUiConfig(cfg);
  // 人设名可能晚于首帧到达：重绘一次座位/状态文案
  if (view) {
    renderSeats(view);
    renderMateZone(view);
    renderStatus(view);
  }
}

/* ---------------- 对局聊天窗口（chat-panel.js 接线） ----------------
 * 独立的第二块聊天面板：GET /chat 回填 + POST /chat 发送 + WS agent_message
 * （断线重连后由 onLive 触发 refresh 补拉，漏帧自愈）。
 * 桌面挂在右栏走子记录下方；窄屏入口挂在动作行下方（CSS 切抽屉）。
 * match.js 只做接线：构造 / 初始化 / 喂 ui-config 与 agent_message 帧。 */

function initChatPanel() {
  chatWin = createChatPanel({
    matchId,
    panelHost: $("side-chat"),
    chipHost: $("chat-dock"),
    getAgentName: () => getHostAgentName(),
    onError: (err, text) => toast(text || errorText(err, "发送失败，请重试"), "error"),
  });
  chatWin.mount();
  chatWin.load().catch((err) => console.warn("聊天记录加载失败", err));
}

/* ---------------- 缺参 / 启动 ---------------- */

function renderMissing(msg) {
  const shell = $("board-shell");
  shell.textContent = "";
  const box = el("div", "board-missing");
  box.appendChild(el("p", null, msg || "缺少对局 ID，无法进入棋局。"));
  const back = el("a", "btn", "返回大厅");
  back.href = "./index.html";
  box.appendChild(back);
  shell.appendChild(box);
  $("seats").textContent = "";
  $("mate-zone").classList.add("hidden");
  $("mate-zone").textContent = "";
  $("actions").textContent = "";
  $("log-list").textContent = "";
  $("status-line").textContent = "—";
  renderConn("missing");
}

/* 离页清理：销毁气泡排队器（清计时器与排队条目，避免离页后定时器继续跑） */
function disposeChat() {
  hideAllSeatBubbles();
  if (chatWin) {
    chatWin.destroy();
    chatWin = null;
  }
}
window.addEventListener("pagehide", disposeChat);

async function bootstrap() {
  if (!matchId) {
    renderMissing();
    return;
  }
  if (spectatorMode()) {
    document.querySelector(".topbar .back")?.remove();
    try {
      await enterSpectator(matchId);
    } catch (err) {
      renderMissing(err.message);
      document.querySelector(".board-missing a")?.remove();
      return;
    }
  }
  initChatPanel();
  conn = new MatchConnection({
    matchId,
    getToken,
    onMatch: applyView,
    onStatus: renderConn,
    onError: (err) => toast(errorText(err, "连接出现问题"), "error"),
    onNotice: (msg) => {
      toast(msg, "info");
      if (spectatorMode() && chatWin) chatWin.applyUiConfig({ chat_enabled: false });
    },
    // 对局聊天的 agent 回复帧：交给聊天窗口追加（座位气泡不受影响）
    onAgentMessage: (frame) => {
      if (chatWin) chatWin.onAgentMessage(frame);
    },
    // WS 每次建连成功（含断线重连）后补拉聊天窗口：治愈断线期间漏掉的
    // agent_message 帧（座位气泡走快照增量，本来就免疫断线）。
    onLive: () => {
      if (chatWin) chatWin.refresh();
    },
  });
  conn.start();
  initUiConfig().catch((err) => console.warn("ui-config 初始化失败", err));
}

/* 全局兜底：任何漏网的异步异常都给 toast，不让页面静默坏掉 */
window.addEventListener("unhandledrejection", (e) => {
  const reason = e && e.reason;
  toast(errorText(reason, "出现了一个小问题，请重试"), "error");
});

bootstrap();
