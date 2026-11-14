/* tests/doudizhu-browser.test.js — 斗地主牌桌 DOM/交互定向测试（node:test，零依赖）。
 *
 * 用最小 DOM 桩驱动真实模块（games/doudizhu/index.js → table.js），覆盖：
 * 1) 叫分：只渲染 legalBids；不叫提交 pass、分数提交数字；
 * 2) 出牌：点选牌 → 点数 move；出牌按钮态随选择变化；
 * 3) 不要：只有 canPass=true 才出现；提示抽屉点选 hint 对应牌；
 * 4) 观战/非本人回合：无手牌或手牌不可点、无操作按钮；
 * 5) 终局：结算面板（分数/春天/炸弹）出现，操作行收起；
 * 6) 销毁幂等、宿主 class 复位。
 *
 * 运行：node --test web/tests/
 */
import test from "node:test";
import assert from "node:assert/strict";

/* ---------------- 最小 DOM 桩 ---------------- */

class FakeClassList {
  constructor(owner) {
    this.owner = owner;
    this.set = new Set();
  }
  set value(v) {
    this.set = new Set(String(v == null ? "" : v).split(/\s+/).filter(Boolean));
  }
  get value() {
    return [...this.set].join(" ");
  }
  add(...names) {
    names.forEach((n) => n && this.set.add(String(n)));
  }
  remove(...names) {
    names.forEach((n) => this.set.delete(String(n)));
  }
  toggle(name, force) {
    const has = this.set.has(name);
    const on = force === undefined ? !has : !!force;
    if (on) this.set.add(name);
    else this.set.delete(name);
    return on;
  }
  contains(name) {
    return this.set.has(name);
  }
}

class FakeEl {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.parentElement = null;
    this.dataset = {};
    this.style = {};
    this.attributes = {};
    this.listeners = new Map();
    this.classList = new FakeClassList(this);
    this.textContentRaw = "";
    this.disabled = false;
    this.title = "";
    this.type = "";
  }
  set className(v) {
    this.classList.value = v;
  }
  get className() {
    return this.classList.value;
  }
  set textContent(v) {
    this.textContentRaw = String(v == null ? "" : v);
    this.children = [];
  }
  get textContent() {
    return this.textContentRaw + this.children.map((c) => c.textContent).join("");
  }
  appendChild(node) {
    if (!node) return node;
    node.parentElement = this;
    this.children.push(node);
    return node;
  }
  addEventListener(type, fn) {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set());
    this.listeners.get(type).add(fn);
  }
  dispatch(type, event = {}) {
    for (const fn of this.listeners.get(type) || []) fn({ target: this, ...event });
  }
  click() {
    if (this.disabled) return;
    this.dispatch("click");
  }
  setAttribute(name, value) {
    this.attributes[name] = String(value);
  }
  getAttribute(name) {
    return this.attributes[name] != null ? this.attributes[name] : null;
  }
  remove() {
    if (this.parentElement) {
      this.parentElement.children = this.parentElement.children.filter((c) => c !== this);
    }
    this.parentElement = null;
  }
}

function walk(node, out = []) {
  out.push(node);
  node.children.forEach((c) => walk(c, out));
  return out;
}

function findAll(root, pred) {
  return walk(root).filter(pred);
}

function byClass(root, cls) {
  return findAll(root, (n) => n.classList.contains(cls));
}

function buttonByText(root, text) {
  return findAll(root, (n) => n.tagName === "BUTTON" && n.textContent === text)[0] || null;
}

function installDom() {
  const prev = { document: globalThis.document };
  globalThis.document = {
    createElement(tag) {
      return new FakeEl(tag);
    },
    createTextNode(text) {
      const n = new FakeEl("#text");
      n.textContent = String(text);
      return n;
    },
  };
  return {
    restore() {
      globalThis.document = prev.document;
    },
  };
}

/* ---------------- fixtures ---------------- */

const HAND = [
  "S3", "H3", "C3", "S4", "D4", "S5", "H6", "C7", "S8",
  "D9", "S10", "HJ", "SQ", "DK", "CA", "S2", "BJ",
];

const SEATS = [
  { kind: "human", name: "阿空(我)", joined: true },
  { kind: "ai", name: "汐祈（Seki）", joined: true },
  { kind: "ai", name: "蕾依（Raiy）", joined: true },
];

const MY_HINTS = [
  { cards: ["S3", "H3", "C3", "S4"], combo: { type: "triple1" }, move: "3 3 3 4", name: "三带一 3" },
  { cards: ["S4", "D4"], combo: { type: "pair" }, move: "4 4", name: "对 4" },
  { cards: ["S5"], combo: { type: "single" }, move: "5", name: "单 5" },
];

function view(over = {}) {
  return {
    viewer: 0,
    you: 0,
    hand: HAND.slice(),
    counts: [17, 17, 17],
    bottom: ["S2", "BJ", "RJ"],
    phase: "play",
    handNo: 1,
    hands: 1,
    firstBidder: 0,
    redeals: 0,
    bid: 3,
    bidder: 0,
    bids: [{ seat: 0, bid: 3 }],
    landlord: 0,
    turn: 0,
    trick: null,
    latest: null,
    recent: [],
    bombs: 0,
    multiplier: 3,
    scores: [0, 0, 0],
    results: [],
    myTurn: true,
    legalBids: [],
    hints: [],
    canPass: false,
    ...over,
  };
}

const matchOf = (v) => ({ id: "dz1", kind: "doudizhu", me: 0, seats: SEATS, view: v });

async function mount(over = {}, opts = { canAct: true, busy: false, over: false, delegated: false }) {
  const dom = installDom();
  const mod = await import("../games/doudizhu/index.js");
  const host = new FakeEl("div");
  const moves = [];
  const ctl = mod.default.mount(host, { onPlace: (p) => moves.push(p) });
  ctl.update(matchOf(view(over)), opts);
  const root = byClass(host, "dz-table")[0];
  return { dom, host, ctl, root, moves, mod };
}

/* ---------------- 叫分 ---------------- */

test("叫分：只渲染 legalBids（不叫/1/2/3），点击提交 pass 或数字；底牌保持牌背", async () => {
  const { dom, root, moves, ctl } = await mount({
    phase: "bid",
    hand: HAND.slice(),
    bottom: null,
    landlord: null,
    turn: 0,
    myTurn: true,
    legalBids: [0, 1, 2, 3],
    hints: [],
    canPass: false,
  });
  try {
    assert.equal(root.dataset.phase, "bid");
    assert.equal(root.dataset.canPlay, "1");
    const bids = byClass(root, "dz-bid");
    assert.deepEqual(bids.map((b) => b.textContent), ["不叫", "1 分", "2 分", "3 分"]);
    // 底牌未公开：3 张牌背，不显示具体牌面
    const bottom = byClass(root, "dz-bottom")[0];
    assert.equal(byClass(bottom, "back").length, 3);
    assert.ok(byClass(bottom, "dz-bottom-label")[0].textContent.includes("未公开"));
    // 三座位阵营片：叫分中
    assert.equal(byClass(root, "dz-role-chip").length, 2, "本人座位不重复进摘要条");
    assert.ok(byClass(root, "dz-role-chip").every((c) => c.textContent.includes("叫分中")));
    // 出牌操作行收起
    assert.ok(byClass(root, "dz-play-row")[0].classList.contains("hidden"));

    bids[2].click();
    bids[0].click();
    assert.deepEqual(moves, [{ move: "2" }, { move: "pass" }]);
    // 重复更新不炸、不重复绑定（幂等渲染）
    ctl.update(matchOf(view({ phase: "bid", legalBids: [0, 3], myTurn: true })), {
      canAct: true, busy: false, over: false, delegated: false,
    });
    assert.deepEqual(byClass(root, "dz-bid").map((b) => b.textContent), ["不叫", "3 分"]);
  } finally {
    dom.restore();
  }
});

/* ---------------- 出牌 / 选中 / 提示 / 不要 ---------------- */

test("出牌：点选 4 张 → move 3 3 3 4；按钮态随选择/忙碌变化", async () => {
  const { dom, root, moves, ctl } = await mount();
  try {
    assert.equal(root.dataset.handCount, "17");
    const rows = byClass(root, "dz-hand-row");
    assert.deepEqual(rows.map((r) => r.children.length), [17], "17 张排成一排扇形叠放（主流斗地主手感）");
    const cards = findAll(root, (n) => n.tagName === "BUTTON" && n.dataset.card);
    assert.equal(cards.length, 17);
    assert.equal(root.dataset.selected, "");
    const play = byClass(root, "dz-play")[0];
    assert.equal(play.disabled, true, "未选牌不可出牌");

    for (const id of ["S3", "H3", "C3", "S4"]) {
      cards.find((c) => c.dataset.card === id).click();
    }
    assert.equal(root.dataset.selected, "3 3 3 4");
    assert.equal(play.disabled, false);
    assert.equal(play.textContent, "出牌（4）");
    assert.equal(byClass(root, "dz-sel")[0].textContent, "已选 4 张");

    // 再点一次取消选中
    cards.find((c) => c.dataset.card === "S4").click();
    assert.equal(root.dataset.selected, "3 3 3");
    cards.find((c) => c.dataset.card === "S4").click();

    play.click();
    assert.deepEqual(moves, [{ move: "3 3 3 4" }]);

    // busy：出牌禁用（动作在途防重复提交）
    ctl.update(matchOf(view()), { canAct: true, busy: true, over: false, delegated: false });
    assert.equal(root.dataset.busy, "1");
    assert.equal(byClass(root, "dz-play")[0].disabled, true);
  } finally {
    dom.restore();
  }
});

test("牌面没有任何破图：插画素材已下线，牌面只靠角标与主花色", async () => {
  const dom = installDom();
  try {
    const { cardChip } = await import("../games/doudizhu/table.js");
    const chip = cardChip("SJ");
    assert.ok(!chip.classList.contains("has-art"), "花牌不再挂插画");
    assert.equal(findAll(chip, (n) => n.tagName === "IMG").length, 0, "没有 img 节点");
    assert.equal(findAll(chip, (n) => n.classList.contains("dz-pip")).length, 1, "主花色仍在");
  } finally {
    dom.restore();
  }
});

test("发牌动画只在首帧/换牌时播放，局面推送重渲染不整手闪烁", async () => {
  const { dom, root, ctl } = await mount();
  try {
    const opts = { canAct: true, busy: false, over: false, delegated: false };
    const rows = () => byClass(root, "dz-hand-row");
    assert.ok(rows()[0].classList.contains("dealing"), "首帧应播放发牌动画");

    // 对手出牌 / 叫分 / 版本推送：手牌不变，只局面变化 → 不再重放
    ctl.update(matchOf(view({ turn: 1, myTurn: false })), opts);
    assert.equal(rows().length, 1);
    assert.ok(!rows()[0].classList.contains("dealing"), "同手牌重渲染不重放发牌动画");

    // 自己出牌后手牌变少 → 也不重放
    ctl.update(matchOf(view({ hand: HAND.slice(0, 12), counts: [12, 17, 17] })), opts);
    assert.ok(!rows()[0].classList.contains("dealing"), "手牌减少不重放");

    // 换牌但张数相同（如春天农民整局未出牌）：局号变化也要重放
    ctl.update(
      matchOf(view({ handNo: 2, hand: HAND.slice(0, 12), counts: [12, 17, 17] })),
      opts
    );
    assert.ok(rows()[0].classList.contains("dealing"), "局号变化重放发牌动画");

    // 换牌（新一副手牌变多）→ 重放发牌动画
    ctl.update(
      matchOf(view({ handNo: 3, hand: HAND.slice(), counts: [17, 17, 17] })),
      opts
    );
    assert.ok(rows()[0].classList.contains("dealing"), "换牌后重放发牌动画");
  } finally {
    dom.restore();
  }
});

test("不要：仅 canPass=true 且本人回合出现；点击提交 pass", async () => {
  const hidden = await mount();
  try {
    assert.equal(byClass(hidden.root, "dz-pass")[0].classList.contains("hidden"), true);
  } finally {
    hidden.dom.restore();
  }

  const shown = await mount({ canPass: true });
  try {
    const pass = byClass(shown.root, "dz-pass")[0];
    assert.equal(pass.classList.contains("hidden"), false);
    pass.click();
    assert.deepEqual(shown.moves, [{ move: "pass" }]);
  } finally {
    shown.dom.restore();
  }

  const notMine = await mount({ canPass: true, turn: 1, myTurn: false });
  try {
    assert.equal(byClass(notMine.root, "dz-pass")[0].classList.contains("hidden"), true, "非本人回合不显示不要");
    assert.ok(byClass(notMine.root, "dz-wait")[0].textContent.includes("等待 汐祈（Seki） 出牌"));
  } finally {
    notMine.dom.restore();
  }
});

test("提示：抽屉点选服务端 hint 的牌；无 hints 时按钮不出现", async () => {
  const noHints = await mount();
  try {
    assert.equal(byClass(noHints.root, "dz-hint")[0].classList.contains("hidden"), true);
  } finally {
    noHints.dom.restore();
  }

  const hinted = await mount({ hints: MY_HINTS });
  try {
    const hintBtn = byClass(hinted.root, "dz-hint")[0];
    assert.equal(hintBtn.classList.contains("hidden"), false);
    assert.equal(byClass(hinted.root, "dz-hints")[0].classList.contains("hidden"), true);
    hintBtn.click();
    const chips = byClass(hinted.root, "dz-hint-chip");
    assert.equal(byClass(hinted.root, "dz-hints")[0].classList.contains("hidden"), false);
    assert.ok(chips[0].textContent.includes("三带一 3"));
    chips[0].click();
    assert.equal(hinted.root.dataset.selected, "3 3 3 4");
    // 点选后抽屉收起，方便直接按出牌
    assert.equal(byClass(hinted.root, "dz-hints")[0].classList.contains("hidden"), true);
    byClass(hinted.root, "dz-play")[0].click();
    assert.deepEqual(hinted.moves, [{ move: "3 3 3 4" }]);
  } finally {
    hinted.dom.restore();
  }
});

/* ---------------- 桌面信息 / 观战 / 终局 ---------------- */

test("桌面：要压的牌、最近动作、底牌公开、倍数与局数", async () => {
  const { dom, root } = await mount({
    turn: 1,
    myTurn: false,
    trick: { seat: 2, cards: ["S10", "HJ", "SQ", "DK", "CA"], combo: { type: "straight" }, name: "顺子 10-A", n: 5 },
    latest: { seat: 2, cards: ["S10", "HJ", "SQ", "DK", "CA"], name: "顺子 10-A", n: 5 },
    recent: [
      { seat: 1, cards: null, name: "不要", n: 0 },
      { seat: 2, cards: ["S10", "HJ", "SQ", "DK", "CA"], name: "顺子 10-A", n: 5 },
    ],
    hands: 3,
    handNo: 2,
    multiplier: 6,
    bombs: 2,
  });
  try {
    assert.ok(byClass(root, "dz-need")[0].textContent.includes("需要压：顺子 10-A"));
    assert.equal(byClass(root, "dz-trick-cards")[0].children.length, 5);
    const rows = byClass(root, "dz-recent-row");
    assert.equal(rows.length, 2);
    assert.ok(rows[0].textContent.includes("蕾依")); // 最新一条是 seat2
    assert.ok(rows[1].textContent.includes("不要"));
    assert.equal(byClass(root, "dz-bottom-cards")[0].children.length, 3);
    assert.ok(byClass(root, "dz-bottom-label")[0].textContent.includes("3 张"));
    assert.equal(byClass(root, "dz-hand-no")[0].textContent, "第 2/3 局");
    assert.equal(byClass(root, "dz-mult")[0].textContent, "6 倍");
    assert.equal(byClass(root, "dz-bombs")[0].textContent, "炸弹 ×2");
  } finally {
    dom.restore();
  }
});

test("观战：无手牌、无操作，状态是「观战中」", async () => {
  const { dom, root } = await mount({ you: null, hand: [] }, { canAct: false, busy: false, over: false, delegated: false });
  try {
    assert.equal(root.dataset.you, "");
    assert.equal(root.dataset.handCount, "0");
    assert.equal(findAll(root, (n) => n.tagName === "BUTTON" && n.dataset.card).length, 0);
    assert.ok(byClass(root, "dz-hand-empty")[0].textContent.includes("观战中"));
    assert.ok(byClass(root, "dz-wait")[0].textContent === "观战中");
    assert.equal(byClass(root, "dz-bid").length, 0);
    assert.equal(byClass(root, "dz-play-row")[0].classList.contains("hidden"), true);
    assert.equal(byClass(root, "dz-pass")[0].classList.contains("hidden"), true);
  } finally {
    dom.restore();
  }
});

test("终局：结算面板出现（地主胜/春天/炸弹/分数），操作行收起", async () => {
  const { dom, root } = await mount(
    {
      phase: "over",
      hand: [],
      counts: [0, 8, 6],
      turn: null,
      myTurn: false,
      landlord: 0,
      bombs: 1,
      multiplier: 6,
      scores: [12, -6, -6],
      results: [
        { hand: 1, landlord: 0, winner: 0, landlordWon: true, bid: 3, bombs: 1, spring: true, antiSpring: false, score: 6, deltas: [12, -6, -6] },
      ],
    },
    { canAct: false, busy: false, over: true, delegated: false }
  );
  try {
    assert.equal(root.classList.contains("is-over"), true);
    const panel = byClass(root, "dz-over")[0];
    assert.equal(panel.classList.contains("hidden"), false);
    assert.equal(byClass(panel, "dz-over-title")[0].textContent, "地主胜");
    assert.ok(panel.textContent.includes("春天"));
    assert.ok(panel.textContent.includes("炸弹 ×1"));
    const rows = byClass(panel, "dz-over-row");
    assert.equal(rows.length, 3);
    assert.ok(rows[0].textContent.includes("+12"));
    assert.ok(rows[1].textContent.includes("-6"));
    assert.ok(byClass(root, "dz-wait")[0].textContent === "本局结束");
    assert.ok(byClass(root, "dz-play-row")[0].classList.contains("hidden"));
  } finally {
    dom.restore();
  }
});

test("隐藏信息：只渲染 view.hand；state 级 cards/rng 即使误入视图也不上桌", async () => {
  const { dom, root } = await mount(
    {
      hand: ["S3"],
      counts: [1, 17, 17],
      bottom: null,
      trick: null,
      latest: [null, { seat: 1, cards: ["H10", "HJ"], name: "对 10" }, null],
      recent: [],
      cards: [["S3"], ["H10", "HJ"], ["D5", "D6"]],
      rng: 123456789,
    },
    { canAct: false, busy: false, over: false, delegated: false }
  );
  try {
    const cards = findAll(root, (n) => n.tagName === "BUTTON" && n.dataset.card);
    assert.deepEqual(cards.map((c) => c.dataset.card), ["S3"], "只渲染本人手牌，不读 state.cards");
    assert.ok(!root.textContent.includes("H10"), "别家手牌不上桌");
    assert.ok(!root.textContent.includes("D5"), "别家手牌不上桌");
    assert.equal(byClass(root, "dz-recent-row").length, 0, "latest 为按座位数组时不臆造历史");
  } finally {
    dom.restore();
  }
});

test("销毁：宿主 class 与内容复位，重复 destroy 幂等", async () => {
  const { dom, host, ctl } = await mount();
  try {
    assert.equal(host.classList.contains("dz-shell"), true);
    assert.ok(host.children.length > 0);
    ctl.destroy();
    assert.equal(host.classList.contains("dz-shell"), false);
    assert.equal(host.children.length, 0);
    ctl.destroy();
  } finally {
    dom.restore();
  }
});
/* ---------------- 花牌/王：无人像插画 ---------------- */

test("花牌/王不渲染人像插画：只保留点数/花色角标（KiraAI 不随插件分发角色图）", async () => {
  const { dom, root } = await mount();
  try {
    const hand = byClass(root, "dz-hand-row")[0];
    assert.ok(hand, "手牌行存在");
    assert.equal(hand.children.filter((c) => c.classList.contains("has-art")).length, 0, "没有任何牌带插画");
    for (const card of hand.children) {
      assert.equal(findAll(card, (n) => n.classList.contains("dz-face-art")).length, 0);
      assert.equal(findAll(card, (n) => n.classList.contains("dz-corner")).length, 2, "上下角标都在");
    }

    const table = await import("../games/doudizhu/table.js");
    const mini = table.cardChip("HQ");
    assert.ok(mini.classList.contains("mini"), "记录/桌面复用迷你尺寸");
    assert.equal(findAll(mini, (n) => n.classList.contains("dz-face-art")).length, 0);
    assert.equal(findAll(mini, (n) => n.classList.contains("dz-corner")).length, 2);
    const joker = table.cardChip("RJ");
    assert.ok(joker.classList.contains("joker"));
    assert.equal(findAll(joker, (n) => n.classList.contains("dz-face-art")).length, 0);
  } finally {
    dom.restore();
  }
});
