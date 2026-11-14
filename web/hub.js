/* hub.js — 游戏中心大厅：游戏卡片 / 继续对局 / 开局 / 进行中棋局操作台 / 联调诊断折叠区。
 *
 * 服务端为唯一真相：/games 返回 { games: [...], current: MatchView | null,
 * active_matches: [...] }（旧后端缺字段时安全降级为空态）。
 * 开局成功：token 存 sessionStorage["gamehub_match_<id>"]，跳转 ./match.html?id=<id>。
 * 「进入操作」：GET /matches/{id}/control 拿 human token → sessionStorage →
 * 跳 match；不修改页面 identity、不迁移 actor、口令不进链接（shared/matches.js）。
 * 页面路径：/page/plugin/kira-plugin-gamehub/hub/（静态托管，相对路径全部相对本目录）。
 * 人设名可配置：/games 的 agent_name 决定「诺里」这一处置渲染成什么（默认仍为诺里）。
 */

import { api, localToken, readSessionToken, writeSessionToken, PLUGIN_ID, API_BASE } from "./shared/api.js";
import { toast } from "./shared/toast.js";
import { fetchIdentity, saveIdentity } from "./shared/chat.js";
import {
  parseActiveMatches,
  isControllable,
  controlBlockedReason,
  sourceLabel,
  noticeSummary,
  turnSummary,
  playerKey,
  kindLabel,
  matchHref,
  enterControl,
} from "./shared/matches.js";
import {
  supportsModes,
  createMatchPayload,
  modeLabel,
  setPersonaName,
  getPersonaName,
  setHostAgentName,
  getHostAgentName,
  personaNameText,
  practiceNameText,
  setAgentAvatar,
  getAgentAvatar,
  levelHint,
  LEVEL_VALUES,
  LEVEL_LABELS,
  LEVEL_HINTS,
  normalizeLevel,
  isPractice,
  lineupOf,
  lineupsOf,
  lineupHint,
  normalizeLineup,
  noriSeatOf,
  seatDisplay,
  AGENT_AVATAR_URL,
} from "./shared/mode.js";
import { cardCoverCandidates } from "./games/registry.js";
import "./shared/atmo.js";

const $ = (id) => document.getElementById(id);

const GAME_NAMES = { gomoku: "五子棋", reversi: "黑白棋", doudizhu: "斗地主", chess: "国际象棋" };

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = String(text); // 一律 textContent，杜绝 HTML 注入
  return n;
}

function asDate(t) {
  if (t == null || t === "") return null;
  const d = t instanceof Date ? t : new Date(t);
  return Number.isNaN(d.getTime()) ? null : d;
}

function fmtRel(t) {
  const d = asDate(t);
  if (!d) return "";
  const s = Math.max(0, (Date.now() - d.getTime()) / 1000);
  if (s < 60) return "刚刚";
  if (s < 3600) return `${Math.floor(s / 60)} 分钟前`;
  if (s < 86400) return `${Math.floor(s / 3600)} 小时前`;
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getMonth() + 1}-${d.getDate()} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function gameName(g) {
  const zh = g && g.name && g.name.zh;
  return zh || (g && g.kind && GAME_NAMES[g.kind]) || (g && g.kind) || "未命名游戏";
}

function isOver(view) {
  if (!view) return false;
  const st = view.status || {};
  if (st.outcome != null && st.outcome !== "") return true;
  const v = view.view;
  // 终局信号按棋种取：五子棋 winLine/full；黑白棋 over；斗地主 phase=over
  return !!(v && (v.winLine || v.full || v.over || v.phase === "over"));
}

function setStatusChip(state) {
  const chip = $("hub-conn");
  const label = $("hub-conn-label");
  chip.className = `conn-chip ${state}`;
  label.textContent = state === "live" ? "已就绪" : state === "offline" ? "离线" : "加载中";
}

/* ---------------- 骨架 ---------------- */

function renderSkeleton() {
  const slot = $("continue-slot");
  slot.classList.remove("hidden");
  slot.textContent = "";
  const sk = el("div", "continue");
  const info = el("div", "continue-info");
  info.appendChild(el("div", "skel skel-bar w30"));
  info.appendChild(el("div", "skel skel-bar tall w50"));
  info.appendChild(el("div", "skel skel-bar w70"));
  sk.appendChild(info);
  sk.appendChild(el("div", "skel skel-bar w30"));
  slot.appendChild(sk);

  renderActiveSkeleton();

  const grid = $("games-grid");
  grid.textContent = "";
  for (let i = 0; i < 3; i++) {
    const card = el("div", "game-card");
    card.style.minHeight = "196px";
    card.appendChild(el("div", "skel skel-bar w30"));
    card.appendChild(el("div", "skel skel-bar tall w50"));
    card.appendChild(el("div", "skel skel-bar w90"));
    card.appendChild(el("div", "skel skel-bar w70"));
    grid.appendChild(card);
  }
}

function renderActiveSkeleton() {
  const slot = $("active-slot");
  slot.classList.remove("hidden");
  const list = $("am-list");
  list.textContent = "";
  for (let i = 0; i < 2; i++) {
    const sk = el("div", "am-card am-skel");
    sk.appendChild(el("div", "skel skel-bar w30"));
    sk.appendChild(el("div", "skel skel-bar tall w50"));
    sk.appendChild(el("div", "skel skel-bar w70"));
    list.appendChild(sk);
  }
}

/* ---------------- 渲染 ---------------- */

function render({ games, current, active_matches: rawActive } = {}) {
  setStatusChip("live");
  renderContinue(current);
  renderActiveMatches(rawActive, current);
  const grid = $("games-grid");
  grid.textContent = "";
  const list = Array.isArray(games) ? games : [];
  if (!list.length) {
    const empty = el("div", "empty");
    empty.appendChild(el("p", "big", "棋室还没有上架游戏"));
    empty.appendChild(el("p", null, `等${getPersonaName()}把棋盘摆好，这里就会出现第一张卡片。`));
    grid.appendChild(empty);
    return;
  }
  const active = current && current.id && !isOver(current) ? current : null;
  list.forEach((g, i) => grid.appendChild(gameCard(g, i, active)));
  // 路线图预告（M4）：纯展示卡，不产生请求；有真实 ready=false 游戏时自然重复也无害
  const have = new Set(list.map((g) => g.kind));
  SOON.filter((g) => !have.has(g.kind)).forEach((g, i) => grid.appendChild(soonCard(g, list.length + i + 1)));
  // 单局提示放最后：它 grid-column 横跨整行，插在卡片中间会把后面的卡片挤到下一行
  if (active) grid.appendChild(activeNote(active));
}

const SOON = [
  { kind: "chess", name: { zh: "国际象棋", en: "Chess" }, family: "棋", blurb: "王车易位与将死，规则完整版筹备中。", glyph: "♞" },
];

function soonCard(g, i) {
  const card = el("article", "game-card not-ready reveal");
  card.dataset.kind = g.kind;
  card.style.animationDelay = `${80 + i * 70}ms`;
  const cover = el("div", "card-cover-wrap card-cover-soon");
  cover.appendChild(el("span", "soon-glyph", g.glyph));
  card.appendChild(cover);
  const top = el("div", "card-top");
  if (g.family) top.appendChild(el("span", "chip family", g.family));
  top.appendChild(el("span", "chip soon", "准备中"));
  card.appendChild(top);
  card.appendChild(el("h3", "game-name", g.name.zh));
  card.appendChild(el("p", "game-en", g.name.en));
  card.appendChild(el("p", "game-blurb", g.blurb));
  const actions = el("div", "card-actions");
  const btn = el("button", "btn", "筹备中");
  btn.type = "button";
  btn.disabled = true;
  actions.appendChild(btn);
  card.appendChild(actions);
  return card;
}

/** 封面图候选链：目录式棋种优先取本游戏目录内的素材（games/<kind>/assets/card-<kind>.png|svg），
 * 再回落公共 assets/card-<kind>.png；全失败自动移除（不塌卡）。新增棋种素材放各自目录。 */
function coverImg(kind, alt, cls, onFail) {
  const list = cardCoverCandidates(kind);
  if (!list.length) return null;
  const img = document.createElement("img");
  img.className = cls;
  img.alt = alt;
  img.loading = "lazy";
  img.decoding = "async"; // 封面为 2MB 级插画：解码不阻塞大厅首屏
  let i = 0;
  img.src = list[i];
  img.addEventListener("error", () => {
    i += 1;
    if (i >= list.length) {
      img.remove();
      if (typeof onFail === "function") onFail();
      return;
    }
    img.src = list[i];
  });
  return img;
}

function renderError(err) {
  setStatusChip("offline");
  $("continue-slot").classList.add("hidden");
  $("active-slot").classList.add("hidden");
  const grid = $("games-grid");
  grid.textContent = "";
  const card = el("div", "game-card error-card");
  card.appendChild(el("p", "big", "棋室暂时打不开"));
  card.appendChild(el("p", null, (err && err.message) || "加载游戏列表失败"));
  const retry = el("button", "btn primary", "重试");
  retry.type = "button";
  retry.addEventListener("click", () => {
    renderSkeleton();
    init();
  });
  card.appendChild(retry);
  grid.appendChild(card);
}

function renderContinue(current) {
  const slot = $("continue-slot");
  slot.textContent = "";
  if (!current || !current.id) {
    slot.classList.add("hidden");
    return;
  }
  slot.classList.remove("hidden");
  const over = isOver(current);

  const card = el("section", "continue reveal");
  const info = el("div", "continue-info");

  const eyebrow = el("p", "continue-eyebrow");
  if (!over) eyebrow.appendChild(el("i", "live-dot"));
  eyebrow.appendChild(document.createTextNode(over ? "上局回顾" : "继续对局"));
  info.appendChild(eyebrow);

  info.appendChild(el("h2", null, gameName(current)));

  const meta = el("p", "continue-meta");
  const pairLabel = lineupOf(current) ? "同桌" : "对手";
  meta.appendChild(document.createTextNode(`${pairLabel}：${opponentName(current)} · ${turnText(current)}`));
  const log = Array.isArray(current.log) ? current.log : [];
  const lastT = log.length ? log[log.length - 1].t : null;
  const rel = fmtRel(lastT);
  if (rel) meta.appendChild(document.createTextNode(` · 更新于 ${rel}`));
  const hasToken = !!readSessionToken(current.id);
  if (!hasToken && !over) meta.appendChild(document.createTextNode(" · 将以观战进入"));
  info.appendChild(meta);

  card.appendChild(info);

  const go = el("a", "btn primary go", over ? "查看棋局" : "进入对局");
  go.href = `./match.html?id=${encodeURIComponent(current.id)}`;
  card.appendChild(go);
  slot.appendChild(card);
}

function opponentName(match) {
  const seats = Array.isArray(match.seats) ? match.seats : [];
  const practice = isPractice(match);
  const lineup = lineupOf(match);
  if (lineup) {
    // 三人桌：同桌名单（名字已由服务端下发）
    const others = seats.filter((s, i) => s && i !== match.me);
    const names = others.map((s) => seatDisplay(s, seats.indexOf(s), practice));
    return names.length ? names.join("、") : "同桌玩家";
  }
  if (typeof match.me === "number" && seats[1 - match.me]) {
    return seatDisplay(seats[1 - match.me], 1 - match.me, practice);
  }
  const other = seats.find((s) => s && (s.kind === "ai" || s.kind === "bot"));
  if (other) return seatDisplay(other, seats.indexOf(other), practice);
  return seats[0] ? seatDisplay(seats[0], 0, practice) : "对手";
}

/* ---------------- 进行中的棋局（M8 统一操作台） ---------------- */

/** active_matches → 棋室全部进行中棋局。旧后端缺字段 → parseActiveMatches
 * 返回空数组 → 走清晰空态。玩家以「对局 ID+座位」为 key，名字仅作展示。 */
function renderActiveMatches(rawActive, current) {
  const slot = $("active-slot");
  const list = $("am-list");
  const count = $("am-count");
  slot.classList.remove("hidden");
  const matches = parseActiveMatches(rawActive);

  if (count) {
    count.textContent = matches.length ? `${matches.length} 局进行中` : "";
    count.classList.toggle("hidden", !matches.length);
  }

  list.textContent = "";
  if (!matches.length) {
    const empty = el("p", "am-empty", "棋室里现在没有进行中的棋局。");
    empty.appendChild(el("span", "am-empty-sub", "有人开局后，这里会列出全部对局：可观战，也可接管操作。"));
    list.appendChild(empty);
    return;
  }

  const myId = current && current.id ? String(current.id) : "";
  matches.forEach((m, i) => {
    const card = activeMatchCard(m, { mine: m.id === myId });
    card.style.animationDelay = `${i * 60}ms`;
    list.appendChild(card);
  });
}

/** 操作台座位身份小片：三人桌按驱动方（turn=人设本人、agent/bot=陪练），
 * 两座位棋类沿用原 kindLabel 语义（逐字段不变）。 */
function activeSeatLabel(p, m) {
  if (m && m.lineup) {
    if (p.human) return "";
    if (p.driver === "turn") return getPersonaName();
    if (p.driver === "agent") return "对手";
    if (p.driver === "bot") return "陪练";
    return "";
  }
  return kindLabel(p.kind, m && m.practice);
}

function activeMatchCard(m, { mine = false } = {}) {
  const card = el("article", "am-card reveal");
  card.dataset.matchId = m.id; // 行 key = 对局 ID，绝不用名字

  const main = el("div", "am-main");

  const eyebrow = el("p", "am-eyebrow");
  eyebrow.appendChild(el("i", "live-dot"));
  eyebrow.appendChild(document.createTextNode(sourceLabel(m.source)));
  main.appendChild(eyebrow);

  const titleRow = el("div", "am-title-row");
  titleRow.appendChild(el("h3", "am-title", m.gameName));
  if (mine) titleRow.appendChild(el("span", "pill you", "我的对局"));
  main.appendChild(titleRow);

  const players = el("div", "am-players");
  m.players.forEach((p) => {
    const chip = el("span", "am-player");
    chip.dataset.key = playerKey(m.id, p); // 对局 ID + 座位号
    // 三人桌用座位序号（没有黑白子语义）；两座位棋类保持原棋子色标
    if (m.lineup) chip.appendChild(el("i", "am-seat-ord", String((p.seat ?? 0) + 1)));
    else chip.appendChild(el("i", `stone-ic sm s${(p.seat ?? 0) + 1}`));
    chip.appendChild(el("span", "am-pname", p.name));
    const kl = activeSeatLabel(p, m);
    if (kl && kl !== p.name) chip.appendChild(el("span", "pill", kl));
    if (p.actor) {
      // 宿主会下发 actor（单管理员固定 admin）；同名玩家靠它对账
      chip.appendChild(el("span", "am-uid", p.actor));
    }
    players.appendChild(chip);
  });
  if (!m.players.length) players.appendChild(el("span", "am-player muted", "座位信息待同步"));
  main.appendChild(players);

  main.appendChild(el("p", "am-turn", turnSummary(m)));

  const ns = noticeSummary(m.notice);
  const noticeLine = el("p", "am-notice");
  noticeLine.appendChild(el("span", "am-notice-label", "通知"));
  noticeLine.appendChild(el("span", "am-notice-val", ns.ready ? ns.platform : "目标未就绪"));
  if (ns.statusText) {
    noticeLine.appendChild(
      el("span", `chip am-notice-status${ns.error ? " bad" : ""}`, ns.statusText)
    );
  }
  if (ns.error) noticeLine.appendChild(el("span", "am-notice-err", ns.error)); // 诊断原样透出（textContent）
  if (m.notice && m.notice.sessionId) noticeLine.title = `通知会话 ${m.notice.sessionId}`;
  main.appendChild(noticeLine);

  card.appendChild(main);

  const actions = el("div", "am-actions");
  const watch = el("a", "btn sm ghost", "观战");
  watch.href = matchHref(m.id);
  actions.appendChild(watch);

  const hasToken = !!readSessionToken(m.id);
  if (mine && hasToken && !m.status.over) {
    // 本人已持座位口令：直接进对局（token 早已在本页会话里）
    const go = el("a", "btn sm primary", "继续对局");
    go.href = matchHref(m.id);
    actions.appendChild(go);
  } else if (isControllable(m)) {
    // 宿主统一操盘：control 端点拿 human token → sessionStorage → 跳 match
    const ctl = el("button", "btn sm primary", "进入操作");
    ctl.type = "button";
    ctl.addEventListener("click", () => onControl(ctl, m.id));
    actions.appendChild(ctl);
  } else {
    const locked = el("button", "btn sm", controlBlockedReason(m));
    locked.type = "button";
    locked.disabled = true;
    actions.appendChild(locked);
  }
  card.appendChild(actions);
  return card;
}

/** 进入操作：在途禁用防重复点击；失败 toast + 恢复按钮（可重试），
 * 并静默刷新列表让终局等状态变化即时反映。 */
async function onControl(btn, matchId) {
  if (btn.disabled) return;
  const prev = btn.textContent;
  btn.disabled = true;
  btn.textContent = "获取权限中…";
  try {
    await enterControl(matchId, {
      api,
      writeToken: writeSessionToken,
      navigate: (href) => {
        location.href = href;
      },
    });
    // 成功路径：页面即将跳转 match，无需恢复按钮
  } catch (err) {
    toast((err && err.message) || "获取操作权限失败，请重试", "error");
    btn.disabled = false;
    btn.textContent = prev;
    refreshGamesQuietly();
  }
}

async function refreshGamesQuietly() {
  try {
    const data = await api("/games");
    applyPersona(data);
    render(data);
  } catch {
    /* 静默：下一次进入/操作时自然刷新 */
  }
}

function turnText(match) {
  if (isOver(match)) {
    const rt = match.status && match.status.resultText;
    return rt || "已结束";
  }
  const st = match.status || {};
  const seats = Array.isArray(match.seats) ? match.seats : [];
  const practice = isPractice(match);
  let wo = typeof st.waitingOn === "number" ? st.waitingOn : null;
  if (wo == null && Array.isArray(st.waitingOn) && st.waitingOn.length === 1 && Number.isInteger(st.waitingOn[0])) {
    wo = st.waitingOn[0];
  }
  const v = match.view;
  if (wo == null && v && (+v.toPlay === 1 || +v.toPlay === 2)) wo = +v.toPlay - 1;
  // 三人桌（斗地主）：当轮座位以 view.turn 为准
  if (wo == null && v && Number.isInteger(+v.turn) && +v.turn >= 0 && (seats.length >= 3 || +v.turn >= 2)) {
    wo = +v.turn;
  }
  const seat = wo != null ? seats[wo] : null;
  if (match.me === wo) return isCardGame(match) ? (String(v && v.phase) === "bid" ? "轮到你叫分" : "轮到你出牌") : "轮到你";
  if (seat) {
    const verb = isCardGame(match) ? (String(v && v.phase) === "bid" ? "叫分中…" : "出牌中…") : "思考中…";
    // 陪练局/教练阵容的座位不是诺里本人（advanced 的 ai 座位也一样）
    const noriSeat = noriSeatOf(match);
    if (noriSeat != null && wo === noriSeat) return `${getPersonaName()}${verb}`;
    return `${seatDisplay(seat, wo, practice)} ${verb}`;
  }
  return "等待中";
}

function isCardGame(match) {
  return String((match && match.kind) || "") === "doudizhu";
}

function gameCard(g, i, active) {
  const card = el("article", "game-card reveal");
  card.dataset.kind = g.kind;
  card.style.animationDelay = `${80 + i * 70}ms`;
  const ready = g.ready !== false;
  if (!ready) card.classList.add("not-ready");

  // 游戏封面插画（目录式棋种先取本游戏目录素材，再回落 assets/card-<kind>.png；
  // 缺失自动隐藏，不阻塞功能）。素材尚未生成时留一块柔和的牌面占位，
  // 不塌卡、不手画 SVG 冒充正式素材。
  const cover = el("div", "card-cover-wrap");
  const coverEl = coverImg(g.kind, `${gameName(g)}封面插画`, "card-cover");
  if (coverEl) {
    coverEl.addEventListener("error", () => cover.classList.add("card-cover-empty"));
    cover.appendChild(coverEl);
  } else {
    cover.classList.add("card-cover-empty");
  }
  card.appendChild(cover);

  const top = el("div", "card-top");
  if (g.family) top.appendChild(el("span", "chip family", g.family));
  if (g.tempo != null && (typeof g.tempo === "string" || typeof g.tempo === "number")) {
    top.appendChild(el("span", "chip", g.tempo));
  }
  if (!ready) top.appendChild(el("span", "chip soon", "准备中"));
  card.appendChild(top);

  card.appendChild(el("h3", "game-name", gameName(g)));
  if (g.name && g.name.en && g.name.en !== gameName(g)) {
    card.appendChild(el("p", "game-en", g.name.en));
  }
  if (g.blurb) card.appendChild(el("p", "game-blurb", g.blurb));

  const meta = el("div", "meta-chips");
  const p = g.players || {};
  if (Number.isFinite(+p.min) && Number.isFinite(+p.max)) {
    const span = +p.min === +p.max ? `${+p.min} 人` : `${+p.min}–${+p.max} 人`;
    meta.appendChild(el("span", "chip", span));
  }
  if (g.has_bot) meta.appendChild(el("span", "chip", "陪练"));
  card.appendChild(meta);

  if (active) {
    // 单 actor 单局：已有未结束对局时，开局入口收敛到顶部「继续对局」卡，
    // 避免「进入对局」与「与人设对战」指向同一局造成重复入口。
    const actions = el("div", "card-actions");
    const same = active.kind === g.kind;
    const locked = el("button", "btn", same ? "对局进行中" : "先结束当前对局");
    locked.type = "button";
    locked.disabled = true;
    actions.appendChild(locked);
    card.appendChild(actions);
    return card;
  }

  if (supportsModes(g) && ready) {
    // 三人桌（斗地主等）：目录声明 lineups → 阵容 + 局数选择器；
    // 两座位棋类：对手三选一（人设本人 / 进阶对手 / 陪练）+ 三档等级。
    if (hasLineups(g)) {
      card.appendChild(lineupBlock(g));
      return card;
    }
    card.appendChild(modeBlock(g));
    return card;
  }

  const actions = el("div", "card-actions");
  const vsAi = el("button", "btn primary", `与${getPersonaName()}对弈`);
  vsAi.type = "button";
  vsAi.disabled = !ready;
  vsAi.addEventListener("click", () => createMatch({ kind: g.kind, opponent: "ai" }, vsAi));
  actions.appendChild(vsAi);

  if (g.has_bot) {
    const vsBot = el("button", "btn", "与陪练对弈");
    vsBot.type = "button";
    vsBot.disabled = !ready;
    vsBot.addEventListener("click", () => createMatch({ kind: g.kind, opponent: "bot" }, vsBot));
    actions.appendChild(vsBot);
  }
  card.appendChild(actions);
  return card;
}

/* ---------------- S2 开局方式选择（回合棋种：人设本人 / 进阶对手 / 陪练 + 三档） ---------------- */

const MODE_STORE_KEY = "gamehub_hub_mode";

/** 选择器状态按游戏类型存本页会话：刷新后保持上次的对手与等级。
 * 旧版本存的是 { mode:"practice"|"nori", difficulty:"easy"|"advanced" }：
 * 这里按驱动兼容映射（advanced=进阶对手、easy=陪练）。 */
function readModeChoice(kind) {
  try {
    const all = JSON.parse(sessionStorage.getItem(MODE_STORE_KEY) || "{}");
    const raw = all && typeof all === "object" ? all[kind] : null;
    const legacy = String((raw && raw.mode) || "");
    let opponent = String((raw && raw.opponent) || "");
    if (opponent !== "nori" && opponent !== "agent" && opponent !== "bot") {
      if (legacy === "nori") opponent = "nori";
      else if (legacy === "practice") opponent = (raw && raw.difficulty) === "advanced" ? "agent" : "bot";
      else opponent = "nori";
    }
    return {
      opponent,
      level: normalizeLevel(raw && raw.level) || "intermediate",
    };
  } catch {
    return { opponent: "nori", level: "intermediate" };
  }
}

function writeModeChoice(kind, choice) {
  try {
    const all = JSON.parse(sessionStorage.getItem(MODE_STORE_KEY) || "{}");
    all[kind] = { opponent: choice.opponent, level: choice.level };
    sessionStorage.setItem(MODE_STORE_KEY, JSON.stringify(all));
  } catch {
    /* 隐私模式等场景忽略 */
  }
}

/** 进阶对手口径：KiraAI 里进阶座位由同一个人设驱动，标签必须与「本人对战」
 * 区分开，所以带「（进阶）」后缀（文案只改这一处，其余沿用默认口径）。 */
function opponentLabel(m, agentName) {
  if (m === "agent") return `与${agentName}对战（进阶）`;
  return modeLabel(m, agentName);
}

function modeBlock(g) {
  const kind = g.kind;
  const choice = readModeChoice(kind);
  // agent_name 就是可配置人设名（默认 "nori" → 渲染「诺里」）
  const agentName = g && g.agent_name ? personaNameText(g.agent_name) : getPersonaName();
  const practiceName = practiceNameText(g && g.practice_name);

  const block = el("div", "mode-block");

  const modeSeg = el("div", "seg mode-seg");
  modeSeg.setAttribute("role", "group");
  modeSeg.setAttribute("aria-label", "对战方式");
  const modeBtns = {};
  for (const m of ["nori", "agent", "bot"]) {
    const b = el("button", "seg-opt", opponentLabel(m, agentName));
    b.type = "button";
    b.dataset.opponent = m;
    b.addEventListener("click", () => {
      choice.opponent = m;
      writeModeChoice(kind, choice);
      sync();
    });
    modeBtns[m] = b;
    modeSeg.appendChild(b);
  }
  block.appendChild(modeSeg);

  // 三档等级：只对人设本人 / 进阶对手显示；陪练是按规则走子的，不摆三档。
  const levelSeg = el("div", "seg level-seg");
  levelSeg.setAttribute("role", "group");
  levelSeg.setAttribute("aria-label", "对手等级");
  const levelBtns = {};
  for (const lv of LEVEL_VALUES) {
    const b = el("button", "seg-opt", LEVEL_LABELS[lv]);
    b.type = "button";
    b.dataset.level = lv;
    b.title = LEVEL_HINTS[lv];
    b.addEventListener("click", () => {
      choice.level = lv;
      writeModeChoice(kind, choice);
      sync();
    });
    levelBtns[lv] = b;
    levelSeg.appendChild(b);
  }
  block.appendChild(levelSeg);

  const hint = el("p", "mode-hint");
  block.appendChild(hint);

  const actions = el("div", "card-actions");
  const start = el("button", "btn primary mode-start", "开始对局");
  start.type = "button";
  start.addEventListener("click", () => {
    if (choice.opponent === "nori") {
      createMatch({ kind, mode: "nori", level: choice.level }, start);
    } else if (choice.opponent === "agent") {
      createMatch({ kind, mode: "practice", difficulty: "advanced", level: choice.level }, start);
    } else {
      createMatch({ kind, mode: "practice", difficulty: "easy" }, start);
    }
  });
  actions.appendChild(start);
  block.appendChild(actions);

  function sync() {
    for (const m of ["nori", "agent", "bot"]) {
      const on = choice.opponent === m;
      modeBtns[m].classList.toggle("on", on);
      modeBtns[m].setAttribute("aria-pressed", String(on));
    }
    const showLevel = choice.opponent !== "bot";
    levelSeg.classList.toggle("hidden", !showLevel);
    for (const lv of LEVEL_VALUES) {
      const on = showLevel && choice.level === lv;
      levelBtns[lv].classList.toggle("on", on);
      levelBtns[lv].setAttribute("aria-pressed", String(on));
    }
    hint.textContent = showLevel
      ? levelHint(choice.level, choice.opponent === "nori" ? getPersonaName() : agentName)
      : levelHint("beginner", practiceName);
  }
  sync();
  return block;
}

/* ---------------- 三人桌开局方式（斗地主：阵容 + 局数） ---------------- */

/** 目录项是否声明了阵容（三人桌）：lineups 非空数组才算；两座位棋类不会写该字段。 */
function hasLineups(g) {
  return Array.isArray(g && g.lineups) && g.lineups.length > 0;
}

const LINEUP_STORE_KEY = "gamehub_hub_lineup";

function readLineupChoice(kind) {
  try {
    const all = JSON.parse(sessionStorage.getItem(LINEUP_STORE_KEY) || "{}");
    const raw = all && typeof all === "object" ? all[kind] : null;
    return {
      lineup: normalizeLineup(raw && raw.lineup) || "nori_agent",
      hands: ["1", "3", "6"].includes(String(raw && raw.hands)) ? String(raw.hands) : "1",
    };
  } catch {
    return { lineup: "nori_agent", hands: "1" };
  }
}

function writeLineupChoice(kind, choice) {
  try {
    const all = JSON.parse(sessionStorage.getItem(LINEUP_STORE_KEY) || "{}");
    all[kind] = { lineup: choice.lineup, hands: choice.hands };
    sessionStorage.setItem(LINEUP_STORE_KEY, JSON.stringify(all));
  } catch {
    /* 隐私模式等场景忽略 */
  }
}

/** 局数选项：后端 choices 可能是 [1,3,6]，也可能是 [{value,label}]（当前模块
 * 用后者，label 如「1 局」）。两种都读，非法值丢弃；全缺回落 1/3/6。 */
function handsChoices(g) {
  const opt = Array.isArray(g && g.options) ? g.options.find((o) => o && o.key === "hands") : null;
  const raw = opt && Array.isArray(opt.choices) ? opt.choices : [];
  const out = [];
  for (const item of raw) {
    const value = String(item && typeof item === "object" ? item.value : item).trim();
    if (!/^[1-9][0-9]*$/.test(value) || out.some((x) => x.value === value)) continue;
    const label = String((item && typeof item === "object" && item.label) || "").trim() || `${value} 局`;
    out.push({ value, label });
  }
  if (out.length) return out;
  return ["1", "3", "6"].map((value) => ({ value, label: `${value} 局` }));
}

function lineupBlock(g) {
  const kind = g.kind;
  const choice = readLineupChoice(kind);
  const agentName = g && g.agent_name ? personaNameText(g.agent_name) : getPersonaName();
  const options = lineupsOf(g, agentName);
  if (!options.some((o) => o.value === choice.lineup)) choice.lineup = options[0].value;

  const block = el("div", "mode-block lineup-block");

  // 阵容：四人一组竖排（文案较长，横排会挤爆窄屏卡片）
  const list = el("div", "lineup-list");
  list.setAttribute("role", "radiogroup");
  list.setAttribute("aria-label", "对局阵容");
  const btns = {};
  options.forEach((opt) => {
    const b = el("button", "lineup-opt");
    b.type = "button";
    b.dataset.lineup = opt.value;
    b.setAttribute("role", "radio");
    b.appendChild(el("span", "lineup-label", opt.label));
    b.appendChild(el("span", "lineup-dot"));
    b.addEventListener("click", () => {
      choice.lineup = opt.value;
      writeLineupChoice(kind, choice);
      sync();
    });
    btns[opt.value] = b;
    list.appendChild(b);
  });
  block.appendChild(list);

  const hint = el("p", "mode-hint");
  block.appendChild(hint);

  // 局数：只在该游戏声明了 options（hands）时渲染
  const handChoices = handsChoices(g);
  if (!handChoices.some((h) => h.value === choice.hands)) choice.hands = handChoices[0].value;
  let syncHands = null;
  if (handChoices.length > 1) {
    const row = el("div", "hands-row");
    row.appendChild(el("span", "hands-label", "局数"));
    const seg = el("div", "seg hands-seg");
    seg.setAttribute("role", "group");
    seg.setAttribute("aria-label", "局数");
    const handBtns = {};
    handChoices.forEach((h) => {
      const b = el("button", "seg-opt");
      b.type = "button";
      b.dataset.hands = h.value;
      b.textContent = h.label;
      b.addEventListener("click", () => {
        choice.hands = h.value;
        writeLineupChoice(kind, choice);
        sync();
      });
      handBtns[h.value] = b;
      seg.appendChild(b);
    });
    row.appendChild(seg);
    block.appendChild(row);

    const actions = el("div", "card-actions");
    const start = el("button", "btn primary mode-start", "开始对局");
    start.type = "button";
    start.addEventListener("click", () =>
      createMatch({ kind, lineup: choice.lineup, hands: choice.hands }, start)
    );
    actions.appendChild(start);
    block.appendChild(actions);

    syncHands = () => {
      handChoices.forEach((h) => {
        const on = choice.hands === h.value;
        handBtns[h.value].classList.toggle("on", on);
        handBtns[h.value].setAttribute("aria-pressed", String(on));
      });
    };
    syncHands();
  } else {
    const actions = el("div", "card-actions");
    const start = el("button", "btn primary mode-start", "开始对局");
    start.type = "button";
    start.addEventListener("click", () => createMatch({ kind, lineup: choice.lineup }, start));
    actions.appendChild(start);
    block.appendChild(actions);
  }

  function sync() {
    options.forEach((opt) => {
      const on = choice.lineup === opt.value;
      const b = btns[opt.value];
      b.classList.toggle("on", on);
      b.setAttribute("aria-checked", String(on));
    });
    hint.textContent = lineupHint(choice.lineup, agentName);
    if (typeof syncHands === "function") syncHands();
  }
  sync();
  return block;
}

function activeNote(active) {
  const note = el("p", "grid-note", `同一时间只进行一局：当前「${gameName(active)}」还没结束，想换游戏请先在对局页认输或下完。`);
  return note;
}

/* ---------------- 开局 ---------------- */

async function createMatch({ kind, mode, difficulty, level, opponent = "ai", lineup, hands }, btn) {
  const prev = btn.textContent;
  btn.disabled = true;
  btn.textContent = "开局中…";
  try {
    const body = createMatchPayload(kind, { mode, difficulty, level, opponent, lineup, hands });
    const data = await api("/matches", { method: "POST", body });
    const m = data && data.match;
    if (!m || !m.id) throw new Error("开局响应缺少对局信息");
    if (data.existing) toast("你已有一局未结束，已为你打开该局", "ok");
    writeSessionToken(m.id, data.token || "");
    location.href = `./match.html?id=${encodeURIComponent(m.id)}`;
  } catch (err) {
    toast((err && err.message) || "开局失败，请稍后再试", "error");
    btn.disabled = false;
    btn.textContent = prev;
  }
}

/* ---------------- M0 诊断（保留接缝，主界面是大厅） ---------------- */

function dlog(line) {
  const pre = $("diag-log");
  const ts = new Date().toLocaleTimeString();
  pre.textContent += `[${ts}] ${line}\n`;
  pre.scrollTop = pre.scrollHeight;
}

function bindDiag() {
  const run = (btnId, fn) => {
    const b = $(btnId);
    b.addEventListener("click", async () => {
      b.disabled = true;
      try {
        await fn();
      } catch (err) {
        dlog(`失败: ${(err && err.message) || err}`);
        toast((err && err.message) || "诊断请求失败", "error");
      } finally {
        b.disabled = false;
      }
    });
  };

  run("diag-ping", async () => {
    const data = await api("/ping");
    dlog(`ping → ${JSON.stringify(data)}`);
  });

  run("diag-session", async () => {
    const data = await api("/page-session");
    dlog(`页面固定会话: ${data.session_id}`);
  });

  run("diag-notice", async () => {
    dlog("发送 notice（回合可能需要 5-15s）…");
    const data = await api("/notice-test", { method: "POST", timeoutMs: 45000 });
    dlog(`notice 已投递: ${JSON.stringify(data)}`);
  });

  dlog(`REST 基址 ${API_BASE}；对局长连接走 /ws/plugin/${PLUGIN_ID}/matches/{id}/ws`);
  if (!localToken()) {
    dlog("未检测到宿主 token（localStorage.jwt_token），请先在宿主 WebUI 登录。");
  }
}

/* ---------------- 人设名（服务端 agent_name） ---------------- */

/** /games 顶层 agent_name 决定人设的渲染名；目录项也带一份，顶层优先。
 * 横幅里的「对坐一局」默认对手是宿主 AI（行动通知回合驱动），所以用
 * /ui-config 的 host_agent_name；拿不到时回落人设名。 */
function applyPersona(data) {
  const name = data && (data.agent_name || (Array.isArray(data.games) && data.games[0] && data.games[0].agent_name));
  setPersonaName(name);
  const hero = $("hero-agent");
  if (hero) hero.textContent = getHostAgentName();
}

/* ---------------- 对手头像（插件自有 agent-avatar，可上传/恢复默认） ----------------
 * 地址由 /ui-config 的 agent_avatar 下发：默认是随插件分发的
 * ./assets/agent-avatar.png；WebUI 上传后变成
 * /api/plugin/kira-plugin-gamehub/agent-avatar?v=<mtime>。
 * 上传流程：文件 → data URL → POST /agent-avatar {"data": ...}；
 * 恢复默认：POST /agent-avatar {"reset": true}。素材文件本身不改。 */

const AVATAR_MAX_BYTES = 4 * 1024 * 1024;
const AVATAR_MIMES = ["image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif"];

function avatarErr(msg) {
  const p = $("avatar-err");
  if (!p) return;
  p.textContent = msg || "";
  p.classList.toggle("hidden", !msg);
}

/** 预览图与共享状态同步（缓存戳由后端 URL 带着走，这里补一次时间戳兜底）。 */
function applyAvatar(url, { bust = false } = {}) {
  const src = setAgentAvatar(url);
  const img = $("avatar-preview");
  if (img) {
    img.src = bust && !src.includes("?") ? `${src}?v=${Date.now()}` : src;
    img.hidden = false;
  }
  return src;
}

function readFileAsDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = () => reject(new Error("读取图片失败"));
    reader.readAsDataURL(file);
  });
}

async function uploadAvatar(file) {
  if (!file) return;
  if (!AVATAR_MIMES.includes(String(file.type || "").toLowerCase())) {
    avatarErr("只支持 PNG / JPG / WebP / GIF 图片。");
    return;
  }
  if (file.size > AVATAR_MAX_BYTES) {
    avatarErr("头像文件过大（上限 4MB）。");
    return;
  }
  avatarErr("");
  const btn = $("avatar-file");
  if (btn) btn.disabled = true;
  try {
    const data = await readFileAsDataUrl(file);
    const res = await api("/agent-avatar", { method: "POST", body: { data }, timeoutMs: 30000 });
    applyAvatar((res && res.agent_avatar) || "", { bust: true });
    toast("头像已更新", "ok");
  } catch (err) {
    avatarErr((err && err.message) || "头像上传失败，请稍后再试");
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function resetAvatar() {
  avatarErr("");
  const btn = $("avatar-reset");
  if (btn) btn.disabled = true;
  try {
    const res = await api("/agent-avatar", { method: "POST", body: { reset: true } });
    applyAvatar((res && res.agent_avatar) || AGENT_AVATAR_URL, { bust: true });
    toast("已恢复默认头像", "ok");
  } catch (err) {
    avatarErr((err && err.message) || "恢复默认头像失败，请稍后再试");
  } finally {
    if (btn) btn.disabled = false;
  }
}

function bindAvatar() {
  const input = $("avatar-file");
  if (input) {
    input.addEventListener("change", () => {
      const file = input.files && input.files[0];
      input.value = ""; // 允许重复选同一文件
      uploadAvatar(file);
    });
  }
  const reset = $("avatar-reset");
  if (reset) reset.addEventListener("click", resetAvatar);
}

/** /ui-config 是头像地址的唯一来源（与对局页同一字段）。 */
async function loadAvatar() {
  try {
    const cfg = await api("/ui-config");
    if (cfg && cfg.agent_name) setPersonaName(cfg.agent_name);
    if (cfg && cfg.host_agent_name) setHostAgentName(cfg.host_agent_name);
    if (cfg) applyAvatar(cfg.agent_avatar);
    const hero = $("hero-agent");
    if (hero) hero.textContent = getHostAgentName();
  } catch {
    applyAvatar(AGENT_AVATAR_URL); // 读不到就显示默认素材
  }
}

/* ---------------- 启动 ---------------- */

async function init() {
  try {
    const data = await api("/games");
    applyPersona(data);
    render(data || {});
  } catch (err) {
    renderError(err);
    toast((err && err.message) || "加载游戏列表失败", "error");
  }
}

window.addEventListener("unhandledrejection", (e) => {
  const reason = e && e.reason;
  toast((reason && reason.message) || "出现了一个小问题，请重试", "error");
});

if (!window.location.pathname.includes(`/page/plugin/${PLUGIN_ID}`)) {
  // 直接以文件方式打开（非宿主路由）时给出提示，但功能照常尝试
  console.warn("游戏中心建议通过宿主页面路由访问：/page/plugin/kira-plugin-gamehub/hub/");
}

renderSkeleton();
init();
bindDiag();
bindIdentity();
bindAvatar();
loadIdentity();
loadAvatar();

/* ---------------- 玩家身份（hero 右侧折叠卡 · 只有显示名） ----------------
 * KiraAI 是单管理员 WebUI：插件身份只有 identity.json 里的一个 name，
 * 没有 user_id、没有会话切换。表单只提交昵称。 */

let identity = null;

function idErr(id, msg) {
  const p = $(id);
  if (!p) return;
  p.textContent = msg || "";
  p.classList.toggle("hidden", !msg);
}

async function loadIdentity() {
  const label = $("identity-label");
  try {
    identity = await fetchIdentity();
  } catch {
    label.textContent = "玩家身份不可用";
    $("identity-meta").textContent = "身份信息读取失败，稍后展开重试。";
    return;
  }
  renderIdentity();
}

function renderIdentity() {
  const d = identity;
  if (!d) return;
  const name = d.effective_name || d.name || "玩家";
  $("identity-label").textContent = `玩家身份 · ${name}`;

  const meta = $("identity-meta");
  meta.textContent = "";
  const row = (k, v) => {
    const line = document.createElement("div");
    line.appendChild(document.createTextNode(`${k}：`));
    const b = document.createElement("b");
    b.textContent = v;
    line.appendChild(b);
    meta.appendChild(line);
  };
  row("棋桌昵称", name);

  $("identity-nick").value = d.name || "";
}

async function saveIdentityForm() {
  if (!identity) return;
  const nick = $("identity-nick").value.trim();
  if (!nick) {
    idErr("identity-nick-err", "昵称不能为空。");
    return;
  }
  if (nick.length > 40) {
    idErr("identity-nick-err", "昵称最长 40 个字。");
    return;
  }
  idErr("identity-nick-err", "");

  const btn = $("identity-save");
  btn.disabled = true;
  const prev = btn.textContent;
  btn.textContent = "保存中…";
  try {
    identity = await saveIdentity({ name: nick });
    renderIdentity();
    toast("身份已保存", "ok");
    // 后端已把座位名改绑到新身份，刷新大厅让「继续对局」等文案同步
    try {
      const data = await api("/games");
      applyPersona(data);
      render(data);
    } catch {
      /* 大厅刷新失败不影响保存结果 */
    }
  } catch (err) {
    toast((err && err.message) || "保存失败，请稍后再试", "error");
  } finally {
    btn.disabled = false;
    btn.textContent = prev;
  }
}

function bindIdentity() {
  if (!$("identity-save")) return;
  $("identity-save").addEventListener("click", saveIdentityForm);
  $("identity-nick").addEventListener("input", () => idErr("identity-nick-err", ""));
}
