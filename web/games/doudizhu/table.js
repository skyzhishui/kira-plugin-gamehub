/* games/doudizhu/table.js — 斗地主牌桌（目录式页面模块的 DOM 层）。
 *
 * 设计口径：
 * - 座位卡（头像/身份/思考/气泡）由 match.js 统一渲染，本模块只负责牌桌本身：
 *   底牌、当前要压的牌、最近动作、我的手牌与操作行。
 * - 点选牌只生成本地选中态；提交时拼成点数文本（logic.selectionMove），
 *   牌型/压牌合法性 100% 交给服务端。
 * - view.hints 是唯一提示来源；没有提示就不发明建议。
 * - 所有文案 textContent 渲染（零 HTML 注入面）。
 */

import {
  actionCards,
  actionName,
  bidLabel,
  bidMove,
  bottomCards,
  bombsText,
  canSubmitPlay,
  cardLabel,
  countOf,
  deltasOf,
  handNoText,
  hintSelection,
  hintsOf,
  isBottomHidden,
  isSpectator,
  lastResult,
  legalBidChoices,
  multiplierText,
  myTurnOf,
  needsText,
  nextHintIndex,
  parseCard,
  passVisible,
  phaseOf,
  pruneSelection,
  recentActions,
  resultTags,
  resultTitle,
  scoresOf,
  seatRoleClass,
  seatRoleLabel,
  selectionMove,
  sortCards,
  toggleSelected,
  trickInfo,
  youOf,
} from "./logic.js";
import { getHostAgentName } from "../../shared/mode.js";

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = String(text);
  return node;
}

function clear(node) {
  if (node) node.textContent = "";
}

/** 当前局号（换牌判定用）；缺失或非数字返回 null。 */
function handNoOf(view) {
  const n = view && view.handNo;
  return typeof n === "number" ? n : null;
}

/** 写 CSS 自定义属性。测试用的 FakeEl 没有 style.setProperty，回退成下标赋值。 */
function setStyleVars(node, vars) {
  const style = node && node.style;
  if (!style) return;
  Object.keys(vars).forEach((name) => {
    const value = String(vars[name]);
    if (typeof style.setProperty === "function") style.setProperty(name, value);
    else style[name] = value;
  });
}

/** 扇形排布参数：--i/--n 给层叠与动画错峰，--rot/--lift 让两端轻微下沉形成弧线。 */
function fanVars(index, count, { rot = 7, lift = 7 } = {}) {
  const n = Math.max(1, Math.trunc(Number(count) || 1));
  const i = Math.max(0, Math.trunc(Number(index) || 0));
  const mid = (n - 1) / 2;
  const t = mid > 0 ? (i - mid) / mid : 0; // -1 … 1
  return {
    "--i": i,
    "--n": n,
    "--rot": `${(t * rot).toFixed(2)}deg`,
    "--lift": `${(Math.abs(t) * lift).toFixed(2)}px`,
  };
}

/** 装饰贴纸：纯展示，不参与信息层级；图挂了就自删，不留破图。 */
function stickerImg(cls, src, alt = "") {
  const node = document.createElement("img");
  node.className = cls;
  node.src = src;
  node.alt = alt;
  node.setAttribute("aria-hidden", "true");
  node.setAttribute("draggable", "false");
  node.addEventListener("error", () => {
    if (node && typeof node.remove === "function") node.remove();
  });
  return node;
}

function seatName(seats, index) {
  const seat = Array.isArray(seats) ? seats[index] : null;
  const name = seat && seat.name != null ? String(seat.name).trim() : "";
  if (name) return name;
  return `玩家 ${Number.isInteger(index) ? index + 1 : "?"}`;
}

/* 花牌/王不使用人像插画：KiraAI 不随插件分发角色立绘，牌面只靠点数/花色
 * 角标识别（牌中央保留大花色兜底）。留空函数便于将来接入通用素材。 */
function faceArtOf() {
  return "";
}

/** 一张牌（face=true）或牌背。mini 用于桌面/底牌/最近动作。 */
function cardNode(card, { mini = false, button = false, onClick = null } = {}) {
  const c = parseCard(card);
  const node = document.createElement(button ? "button" : "span");
  // 手牌、桌面打出的牌、底牌、最近动作与对局记录共用同一套牌面；
  // 花牌/王的插画在迷你尺寸也保留，保证桌上/记录里同样能认出是哪张。
  const art = faceArtOf(c);
  node.className = `dz-card${mini ? " mini" : ""}${c && c.red ? " red" : ""}${c && c.joker ? " joker" : ""}${art ? " has-art" : ""}`;
  if (button) {
    node.type = "button";
    node.dataset.card = c ? c.id : String(card);
    if (onClick) node.addEventListener("click", onClick);
  }
  if (!c) {
    node.appendChild(el("span", "dz-pip", "?"));
    return node;
  }
  if (art) {
    const img = document.createElement("img");
    img.className = "dz-face-art";
    img.src = `./games/doudizhu/assets/${art}.png`;
    img.alt = "";
    img.setAttribute("aria-hidden", "true");
    // 素材 404 时不留下破图：移除插画并让中央主花色兜底显示（角标一直在）。
    img.addEventListener("error", () => {
      if (node.classList) node.classList.remove("has-art");
      if (typeof img.remove === "function") img.remove();
    });
    node.appendChild(img);
  }
  const corner = el("span", "dz-corner");
  corner.appendChild(el("b", "dz-rank", c.joker ? c.rankText : c.rank));
  if (c.suitSymbol) corner.appendChild(el("i", "dz-suit", c.suitSymbol));
  node.appendChild(corner);
  // 右下角镜像角标（旋转 180° 由 CSS 负责）；迷你牌也保留，
  // 桌面打出的牌/底牌/记录里同样能从两个方向读出点数与花色。
  const foot = el("span", "dz-corner bottom");
  foot.appendChild(el("b", "dz-rank", c.joker ? c.rankText : c.rank));
  if (c.suitSymbol) foot.appendChild(el("i", "dz-suit", c.suitSymbol));
  node.appendChild(foot);
  node.appendChild(el("span", "dz-pip", c.joker ? "王" : c.suitSymbol));
  node.title = c.joker ? c.rankText : `${c.suitName}${c.rank}`;
  node.setAttribute("aria-label", node.title);
  return node;
}

function cardBack(mini = false) {
  const node = el("span", `dz-card back${mini ? " mini" : ""}`);
  node.setAttribute("aria-hidden", "true");
  return node;
}

/** 供对局记录等页面级位置复用的牌面节点（默认迷你尺寸，含花牌/王插画）。 */
export function cardChip(card, { mini = true } = {}) {
  return cardNode(card, { mini });
}

export function mountTable(host, { onPlace } = {}) {
  if (!host) return { update() {}, destroy() {} };
  // 只移除本模块上一次挂载的牌桌；保留 #board-skel / #sticker-slot 等页面槽位
  // （整块 textContent="" 会把结果层/骨架一起清掉）。
  [...host.children].forEach((child) => {
    if (child && child.classList && child.classList.contains("dz-table")) child.remove();
  });
  host.classList.add("dz-shell");

  const root = el("div", "dz-table");
  root.dataset.kind = "doudizhu";

  /* 顶部小条：第几局 / 倍数 / 炸弹 */
  const banner = el("div", "dz-banner");
  const handNoEl = el("span", "dz-hand-no", "第 1 局");
  const multEl = el("span", "dz-pill dz-mult", "1 倍");
  const baseEl = el("span", "dz-pill dz-base hidden");
  const bombsEl = el("span", "dz-pill dz-bombs hidden");
  const redealEl = el("span", "dz-pill dz-redeals hidden");
  const seatRolesEl = el("div", "dz-seat-roles");
  const bidHistoryEl = el("div", "dz-bid-history hidden");
  banner.appendChild(handNoEl);
  banner.appendChild(multEl);
  banner.appendChild(baseEl);
  banner.appendChild(bombsEl);
  banner.appendChild(redealEl);
  // 顶部信息收进一块「奶油纸带」：局数/倍数/阵营/叫分记录都在木桌之上更好读。
  const header = el("div", "dz-header");
  header.appendChild(banner);
  header.appendChild(seatRolesEl);
  header.appendChild(bidHistoryEl);
  header.appendChild(stickerImg("dz-header-paw", "./games/doudizhu/assets/sticker-paw.png"));
  root.appendChild(header);

  /* 桌面主体 */
  const felt = el("div", "dz-felt");
  const bottomWrap = el("div", "dz-bottom");
  const bottomLabel = el("span", "dz-bottom-label", "底牌");
  const bottomCardsEl = el("div", "dz-bottom-cards");
  bottomWrap.appendChild(bottomLabel);
  bottomWrap.appendChild(bottomCardsEl);
  felt.appendChild(bottomWrap);

  const stage = el("div", "dz-stage");
  const needEl = el("p", "dz-need", "");
  const trickEl = el("div", "dz-trick");
  stage.appendChild(needEl);
  stage.appendChild(trickEl);
  felt.appendChild(stage);

  const recentBox = el("div", "dz-recent", "");
  felt.appendChild(recentBox);

  // 装饰贴纸（爪印 / 星点）：纯视觉，不挡信息，不抢点击。
  felt.appendChild(stickerImg("dz-sticker dz-sticker-paw", "./games/doudizhu/assets/sticker-paw.png"));
  felt.appendChild(stickerImg("dz-sticker dz-sticker-sparkle", "./games/doudizhu/assets/sticker-sparkle.png"));

  const overBox = el("div", "dz-over hidden");
  felt.appendChild(overBox);
  root.appendChild(felt);

  /* 我方区域：手牌 + 操作 */
  const meBox = el("div", "dz-me");
  const meHead = el("div", "dz-me-head");
  const meRole = el("span", "dz-pill dz-me-role");
  const meName = el("span", "dz-me-name", "我");
  const selEl = el("span", "dz-sel", "点牌选中");
  meHead.appendChild(meName);
  meHead.appendChild(meRole);
  meHead.appendChild(selEl);
  meBox.appendChild(meHead);

  const handEl = el("div", "dz-hand");
  meBox.appendChild(handEl);

  const actions = el("div", "dz-actions");
  const bidsEl = el("div", "dz-bids");
  const playRow = el("div", "dz-play-row");
  const hintBtn = el("button", "btn dz-hint", "提示");
  hintBtn.type = "button";
  hintBtn.classList.add("hidden");
  const passBtn = el("button", "btn dz-pass", "不要");
  passBtn.type = "button";
  const playBtn = el("button", "btn primary dz-play", "出牌");
  playBtn.type = "button";
  playRow.appendChild(hintBtn);
  playRow.appendChild(passBtn);
  playRow.appendChild(playBtn);
  const waitEl = el("p", "dz-wait", "");
  actions.appendChild(bidsEl);
  actions.appendChild(playRow);
  actions.appendChild(waitEl);
  meBox.appendChild(actions);

  const hintsBox = el("div", "dz-hints hidden");
  meBox.appendChild(hintsBox);
  root.appendChild(meBox);

  host.appendChild(root);

  /* ---------------- 本地状态 ---------------- */
  let current = null; // MatchView
  let selected = new Set();
  let opts = { canAct: false, busy: false, over: false, delegated: false };
  let you = null;
  let hand = [];
  let cardButtons = new Map();
  let hints = [];
  let hintIndex = -1;
  let hintsOpen = false;
  let lastSig = "";
  // 上一帧手牌张数与局号：只有首帧或换牌（局号变化/手牌变多）才播发牌
  // 动画，对手出牌/叫分/版本推送引起的重渲染不再整手闪烁。
  let lastHandSize = -1;
  let lastHandNo = null;

  function currentView() {
    return current && current.view ? current.view : null;
  }

  function interactive() {
    const view = currentView();
    if (!view || opts.busy || opts.over) return false;
    return !!opts.canAct && myTurnOf(view, you);
  }

  function submit(move) {
    if (!move || typeof onPlace !== "function" || !interactive()) return;
    onPlace({ move });
  }

  /* ---------------- 渲染 ---------------- */

  function renderBanner(view) {
    handNoEl.textContent = handNoText(view);
    // 观战看不到「轮到你」，顶部写清「观战 · 第几局」更直观
    if (isSpectator(view, current)) handNoEl.textContent = `${handNoText(view)} · 观战`;
    multEl.textContent = multiplierText(view);
    const bt = bombsText(view);
    bombsEl.textContent = bt;
    bombsEl.classList.toggle("hidden", !bt);
    const base = Math.trunc(Number(view.bid) || 0);
    const bidding = phaseOf(view) === "bid";
    baseEl.textContent = base > 0 ? (bidding ? `当前 ${base} 分` : `底分 ${base}`) : "";
    baseEl.classList.toggle("hidden", base <= 0);
    const redeals = Math.trunc(Number(view.redeals) || 0);
    redealEl.textContent = redeals > 0 ? `重发 ×${redeals}` : "";
    redealEl.classList.toggle("hidden", redeals <= 0);

    // 叫分记录（服务端 bids 唯一来源）：只做旁白，不参与规则。
    const bids = Array.isArray(view.bids) ? view.bids : [];
    const shownBids = bids.filter((b) => b && Number.isInteger(Number(b.bid))).slice(-4);
    const overNow = phaseOf(view) === "over";
    bidHistoryEl.classList.toggle("hidden", overNow || !shownBids.length);
    clear(bidHistoryEl);
    if (!overNow && shownBids.length) {
      bidHistoryEl.appendChild(el("span", "dz-bid-history-label", "叫分"));
      shownBids.forEach((b) => {
        const seat = Number(b.seat);
        bidHistoryEl.appendChild(
          el(
            "span",
            "dz-bid-history-item",
            `${seatName(current.seats, seat)} ${bidLabel(Number(b.bid))}`
          )
        );
      });
    }

    // 三座位阵营：一眼看清谁是地主/农民/叫分中
    const seats = Array.isArray(current && current.seats) ? current.seats : [];
    clear(seatRolesEl);
    seats.forEach((seat, i) => {
      // 本人座位在座位卡上已写明「你 + 阵营 + 张数」，摘要条不重复
      if (i === you) return;
      const role = seatRoleLabel(view, i);
      if (!role) return;
      const chip = el("span", `dz-role-chip ${seatRoleClass(view, i)}`);
      chip.dataset.seat = String(i);
      chip.appendChild(el("i", "dz-role-dot"));
      chip.appendChild(document.createTextNode(seatName(seats, i)));
      chip.appendChild(el("b", null, role));
      const count = countOf(view, i);
      if (count != null) chip.appendChild(el("span", "dz-role-count", `${count} 张`));
      seatRolesEl.appendChild(chip);
    });
  }

  function renderBottom(view) {
    clear(bottomCardsEl);
    const cards = bottomCards(view);
    bottomLabel.textContent = isBottomHidden(view) ? "底牌 · 未公开" : `底牌 · ${cards.length} 张`;
    if (isBottomHidden(view)) {
      for (let i = 0; i < 3; i += 1) {
        const node = cardBack(true);
        setStyleVars(node, fanVars(i, 3, { rot: 4, lift: 2 }));
        bottomCardsEl.appendChild(node);
      }
      bottomWrap.classList.add("hidden-cards");
      return;
    }
    bottomWrap.classList.remove("hidden-cards");
    cards.forEach((c, i) => {
      const node = cardNode(c, { mini: true });
      setStyleVars(node, fanVars(i, cards.length, { rot: 4, lift: 2 }));
      bottomCardsEl.appendChild(node);
    });
  }

  function renderStage(view) {
    const info = trickInfo(view);
    clear(trickEl);
    needEl.textContent = needsText(view, you);
    // 本人先手（桌上没有要压的牌）时毡面中央只剩一句「你先出牌」，
    // 不再留一张空桌子：空态收成一行，给底部的手牌排与抬起牌腾出空间。
    stage.classList.toggle("dz-stage-empty", !info && !needEl.textContent);
    if (!info) {
      trickEl.classList.add("empty");
      return;
    }
    trickEl.classList.remove("empty");
    const who = el("span", "dz-trick-who", seatName(current.seats, info.seat));
    trickEl.appendChild(who);
    const cards = el("div", "dz-trick-cards");
    const shown = info.cards.slice(0, 12);
    shown.forEach((c, i) => {
      const node = cardNode(c, { mini: true });
      setStyleVars(node, fanVars(i, shown.length, { rot: 4, lift: 2 }));
      cards.appendChild(node);
    });
    if (info.cards.length > 12) cards.appendChild(el("span", "dz-more", `+${info.cards.length - 12}`));
    trickEl.appendChild(cards);
    trickEl.appendChild(el("span", "dz-trick-name", info.name));
  }

  function renderRecent(view) {
    clear(recentBox);
    // 牌桌内只留最近两手作旁白；完整动作顺序在右侧「对局记录」。
    // 四行会与牌面/贴纸抢高度，也容易在窄屏叠压。
    const rows = recentActions(view, 2);
    if (!rows.length) {
      recentBox.appendChild(el("span", "dz-recent-empty", "出牌后会显示在这里"));
      return;
    }
    const info = trickInfo(view);
    rows.forEach((action, i) => {
      const row = el("div", `dz-recent-row${i === 0 ? " latest" : ""}`);
      row.dataset.seat = String(action && action.seat != null ? action.seat : "");
      row.appendChild(el("span", "dz-recent-who", seatName(current.seats, Number(action && action.seat))));
      const cards = actionCards(action);
      // 当前要压的那一手已在牌桌中央展示（牌面 + 牌型），最近动作里同一条只留
      // 「谁 + 牌型名」旁白，不再重复整组牌面。
      const isTrick = i === 0 && info && info.seat != null && info.seat === Number(action && action.seat);
      if (cards.length && !isTrick) {
        const box = el("span", "dz-recent-cards");
        const shown = cards.slice(0, 8);
        shown.forEach((c, j) => {
          const node = cardNode(c, { mini: true });
          setStyleVars(node, fanVars(j, shown.length, { rot: 3, lift: 1 }));
          box.appendChild(node);
        });
        if (cards.length > 8) box.appendChild(el("span", "dz-more", `+${cards.length - 8}`));
        row.appendChild(box);
        row.appendChild(el("span", "dz-recent-name", actionName(action)));
      } else if (cards.length) {
        row.appendChild(el("span", "dz-recent-name", actionName(action)));
      } else {
        row.appendChild(el("span", "dz-recent-pass", actionName(action)));
      }
      recentBox.appendChild(row);
    });
  }

  function renderOver(view) {
    const over = phaseOf(view) === "over" || opts.over;
    overBox.classList.toggle("hidden", !over);
    // 终局：桌面区不再需要底牌/要压的牌/最近动作占位，整块让给结算面板
    bottomWrap.classList.toggle("hidden", over);
    stage.classList.toggle("hidden", over);
    recentBox.classList.toggle("hidden", over);
    if (!over) {
      clear(overBox);
      return;
    }
    clear(overBox);
    overBox.appendChild(stickerImg("dz-over-sparkle", "./games/doudizhu/assets/sticker-sparkle.png"));
    const result = lastResult(view);
    overBox.appendChild(el("p", "dz-over-title", resultTitle(result)));
    const tags = resultTags(result);
    if (tags.length) {
      const tagRow = el("div", "dz-over-tags");
      tags.forEach((t) => tagRow.appendChild(el("span", "dz-pill", t)));
      overBox.appendChild(tagRow);
    }
    const scores = scoresOf(view);
    const deltas = deltasOf(result);
    const seats = Array.isArray(current && current.seats) ? current.seats : [];
    const list = el("div", "dz-over-scores");
    const n = Math.max(scores.length, deltas.length, seats.length);
    for (let i = 0; i < n; i += 1) {
      const row = el("div", "dz-over-row");
      row.dataset.seat = String(i);
      row.appendChild(el("span", "dz-over-who", seatName(seats, i)));
      const delta = deltas[i];
      if (Number.isFinite(delta)) {
        row.appendChild(
          el("span", `dz-over-delta${delta > 0 ? " plus" : delta < 0 ? " minus" : ""}`, `${delta > 0 ? "+" : ""}${delta}`)
        );
      }
      row.appendChild(el("span", "dz-over-total", `${scores[i] != null ? scores[i] : 0} 分`));
      list.appendChild(row);
    }
    overBox.appendChild(list);
    const rt = String((current && current.status && current.status.resultText) || "").trim();
    if (rt) overBox.appendChild(el("p", "dz-over-note", rt));
  }

  function renderHand(view) {
    clear(handEl);
    cardButtons = new Map();
    if (!hand.length) {
      lastHandSize = 0;
      lastHandNo = handNoOf(view);
      const empty = el("p", "dz-hand-empty", isSpectator(view, current) ? "观战中，看不到手牌" : "等待发牌…");
      handEl.appendChild(empty);
      return;
    }
    const playable = interactive();
    // 主流斗地主的做法：单手牌一排扇形叠放，靠负外边距让每张只露出角标。
    const sorted = sortCards(hand);
    // 只有首帧或换牌才播发牌动画；换牌以局号变化为准（春天地主/农民未出牌
    // 时张数可能相同），其余重渲染不再整手重放（避免靠右的牌长时间透明）。
    const handNo = handNoOf(view);
    const dealing =
      lastHandSize < 0 ||
      sorted.length > lastHandSize ||
      (handNo != null && lastHandNo != null && handNo !== lastHandNo);
    lastHandSize = sorted.length;
    lastHandNo = handNo;
    const row = el("div", "dz-hand-row");
    row.dataset.row = "0";
    if (dealing) row.classList.add("dealing");
    sorted.forEach((card, index) => {
      const id = String(card).toUpperCase();
      const node = cardNode(card, {
        button: true,
        onClick: () => {
          if (!interactive()) return;
          selected = toggleSelected(selected, id);
          syncSelection();
        },
      });
      node.disabled = !playable;
      node.dataset.row = "0";
      setStyleVars(node, fanVars(index, sorted.length, { rot: 7, lift: 7 }));
      cardButtons.set(id, node);
      row.appendChild(node);
    });
    handEl.appendChild(row);
    syncSelection();
  }

  function syncSelection() {
    const view = currentView();
    cardButtons.forEach((node, id) => {
      const on = selected.has(id);
      node.classList.toggle("selected", on);
      node.setAttribute("aria-pressed", String(on));
    });
    const n = selected.size;
    selEl.textContent = n ? `已选 ${n} 张` : "点牌选中";
    root.dataset.selected = selectionMove([...selected]);
    playBtn.disabled = !view || !canSubmitPlay(view, selected, { canAct: opts.canAct, busy: opts.busy, you });
    playBtn.textContent = n ? `出牌（${n}）` : "出牌";
  }

  function renderBids(view) {
    clear(bidsEl);
    const choices = legalBidChoices(view);
    const my = myTurnOf(view, you) && !!opts.canAct && !opts.busy && !opts.over && !opts.delegated;
    bidsEl.classList.toggle("hidden", phaseOf(view) !== "bid" || !choices.length || opts.delegated);
    if (phaseOf(view) !== "bid" || !choices.length || opts.delegated) return;
    choices.forEach((bid) => {
      const node = el("button", `btn dz-bid${bid === 0 ? " ghost" : bid === 3 ? " strong" : ""}`, bidLabel(bid));
      node.type = "button";
      node.dataset.bid = String(bid);
      node.disabled = !my;
      node.addEventListener("click", () => submit(bidMove(bid)));
      bidsEl.appendChild(node);
    });
  }

  function renderControls(view) {
    const phase = phaseOf(view);
    const my = myTurnOf(view, you);
    const spectator = isSpectator(view, current);
    // 操作行只在「本人可出牌」时出现；对手回合/代打中不摆一排禁用按钮，
    // 用一句等待说明替代（按钮态切换不改变牌桌高度之外的结构）。
    const showPlayRow = phase === "play" && my && !spectator && !opts.delegated;
    playRow.classList.toggle("hidden", !showPlayRow);
    waitEl.classList.toggle("hidden", showPlayRow);
    hintsBox.classList.toggle("hidden", !hintsOpen);
    hintBtn.classList.toggle("hidden", phase !== "play" || !hints.length);

    if (spectator) {
      waitEl.textContent = "观战中";
    } else if (phase === "over") {
      waitEl.textContent = "本局结束";
    } else if (opts.delegated && my) {
      // 代打方是宿主主 agent（行动通知回合驱动），名字随配置走，不写死
      const who = getHostAgentName();
      waitEl.textContent =
        phase === "bid" ? `${who}正在替你叫分…` : `${who}正在替你出牌…`;
    } else if (phase === "bid") {
      waitEl.textContent = my
        ? legalBidChoices(view).length
          ? ""
          : "等待可叫的分数…"
        : "等待其他玩家叫分…";
      waitEl.classList.toggle("hidden", !waitEl.textContent);
    } else if (phase === "play" && !my) {
      waitEl.textContent = `等待 ${seatName(current.seats, Number(view.turn))} 出牌…`;
    } else if (opts.busy) {
      waitEl.textContent = "正在提交…";
    } else {
      waitEl.textContent = "";
    }

    passBtn.classList.toggle("hidden", !passVisible(view, { canAct: opts.canAct, busy: opts.busy, you }));
    syncSelection();
  }

  function renderHints(view) {
    clear(hintsBox);
    const canHint =
      phaseOf(view) === "play" && hints.length && !opts.busy && !opts.over && !opts.delegated && myTurnOf(view, you) && !!opts.canAct;
    if (!canHint) hintsOpen = false;
    hintsBox.classList.toggle("hidden", !hintsOpen || !canHint);
    hintBtn.classList.toggle("hidden", !canHint);
    if (!hintsOpen || !canHint) return;
    hints.slice(0, 12).forEach((hint, i) => {
      const node = el("button", `dz-hint-chip${i === hintIndex ? " on" : ""}`);
      node.type = "button";
      node.dataset.hint = String(i);
      node.appendChild(el("span", "dz-hint-name", hint.name));
      if (hint.cards.length) {
        node.appendChild(el("span", "dz-hint-cards", hint.cards.slice(0, 6).map(cardLabel).join(" ")));
      }
      node.addEventListener("click", () => applyHint(i));
      hintsBox.appendChild(node);
    });
  }

  function applyHint(index) {
    const hint = hints[index];
    if (!hint) return;
    hintIndex = index;
    const picked = hintSelection(hint, hand);
    if (picked.size) {
      selected = picked;
      hintsOpen = false;
      syncSelection();
      renderHints(currentView());
      return;
    }
    // 没有具体牌（服务端只给 move）：直接按服务端的建议提交，前端不猜牌型
    submit(hint.move);
  }

  hintBtn.addEventListener("click", () => {
    const view = currentView();
    if (!view || !hints.length) return;
    if (!hintsOpen) {
      hintsOpen = true;
      renderHints(view);
      return;
    }
    hintIndex = nextHintIndex(hintIndex, hints.length);
    applyHint(hintIndex);
  });

  passBtn.addEventListener("click", () => submit("pass"));
  playBtn.addEventListener("click", () => {
    const move = selectionMove([...selected]);
    if (move) submit(move);
  });

  /* ---------------- 生命周期 ---------------- */

  function update(match, nextOpts = {}) {
    if (!match || !match.view) return;
    // 聊天/气泡帧会带同一份局面重复推送：局面与操作权都没变时不再重建牌桌，
    // 避免键盘焦点丢失、手牌入场动画重播（selected 由本地点选维护）。
    const next = {
      canAct: !!nextOpts.canAct,
      busy: !!nextOpts.busy,
      over: !!nextOpts.over,
      delegated: !!nextOpts.delegated,
    };
    const sig = JSON.stringify({
      view: match.view,
      seats: (Array.isArray(match.seats) ? match.seats : []).map((s) => [s && s.kind, s && s.name]),
      opts: next,
    });
    if (sig === lastSig) {
      current = match;
      return;
    }
    lastSig = sig;
    current = match;
    opts = next;
    const view = match.view;
    you = youOf(view, match);
    hand = Array.isArray(view.hand) ? view.hand.filter((c) => parseCard(c)) : [];
    selected = pruneSelection(selected, hand);
    if (!myTurnOf(view, you) || phaseOf(view) !== "play") selected.clear();
    hints = phaseOf(view) === "play" ? hintsOf(view) : [];
    if (!hints.length) {
      hintsOpen = false;
      hintIndex = -1;
    } else if (hintIndex >= hints.length) {
      hintIndex = -1;
    }

    root.dataset.phase = phaseOf(view);
    root.dataset.turn = String(view.turn != null ? view.turn : "");
    root.dataset.you = you == null ? "" : String(you);
    root.dataset.handCount = String(hand.length);
    root.dataset.canPlay = interactive() ? "1" : "0";
    root.dataset.canPass = view.canPass === true ? "1" : "0";
    root.dataset.busy = opts.busy ? "1" : "0";
    root.classList.toggle("spectator", you == null);
    root.classList.toggle("is-over", phaseOf(view) === "over" || opts.over);

    meName.textContent = you == null ? "观战" : seatName(match.seats, you);
    meRole.textContent = you == null ? "" : seatRoleLabel(view, you);
    meRole.className = `dz-pill dz-me-role${you == null ? " hidden" : ` ${seatRoleClass(view, you)}`}`;

    renderBanner(view);
    renderBottom(view);
    renderStage(view);
    renderRecent(view);
    renderOver(view);
    renderHand(view);
    renderBids(view);
    renderHints(view);
    renderControls(view);
  }

  function destroy() {
    host.classList.remove("dz-shell");
    [...host.children].forEach((child) => {
      if (child && child.classList && child.classList.contains("dz-table")) child.remove();
    });
    current = null;
    selected = new Set();
    cardButtons = new Map();
    hints = [];
    lastSig = "";
    lastHandSize = -1;
    lastHandNo = null;
  }

  return { update, destroy };
}