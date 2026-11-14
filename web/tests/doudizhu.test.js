/* tests/doudizhu.test.js — 斗地主前端纯逻辑 + 三人桌模式契约（node:test，零依赖）。
 *
 * 覆盖：
 * 1) 牌 ID 解析/展示（S3/H10/DA/C2/BJ/RJ）、排序、分行、点选 → move 串；
 * 2) 视角/回合（view.you 与 view.myTurn 优先，观战不接操作）；
 * 3) 叫分按钮（合法分数唯一来源 legalBids）、不叫=pass；
 * 4) hints 点选、canPass、操作闸门；
 * 5) 局面文案（地主/农民/叫分中、要压的牌、最近动作、结算）；
 * 6) 三人桌阵容载荷（四种 lineup / seat_drivers / nori_seat / nori_role）与
 *    两座位旧行为逐字段不变；
 * 7) registry：目录式模块加载与封面素材候选。
 *
 * 运行：node --test web/tests/
 */
import test from "node:test";
import assert from "node:assert/strict";

import {
  actionCards,
  bidLabel,
  bidMove,
  bottomCards,
  canSubmitPlay,
  cardLabel,
  cardMoveText,
  countOf,
  deltasOf,
  handNoText,
  handRows,
  hintSelection,
  hintsOf,
  isBottomHidden,
  isPassAction,
  isSpectator,
  lastResult,
  legalBidChoices,
  moveText,
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
} from "../games/doudizhu/logic.js";
import doudizhu from "../games/doudizhu/index.js";
import {
  createMatchPayload,
  delegatedTurnStatus,
  canDelegate,
  isCoachMatch,
  lineupHint,
  lineupOf,
  lineupsOf,
  matchOver,
  noriRoleOf,
  noriSeatOf,
  restartPayload,
  seatDriverOf,
  seatIdentity,
  seatIdentityLabel,
} from "../shared/mode.js";
import { DIR_MODULES, cardCoverCandidates, loadGameModule, moduleUrls } from "../games/registry.js";

/* ---------------- fixtures ---------------- */

const HAND = [
  "S3", "H3", "C3", "S4", "D4", "S5", "H6", "C7", "S8",
  "D9", "S10", "HJ", "SQ", "DK", "CA", "S2", "BJ",
];

const SEATS = [
  { kind: "human", name: "阿空(我)", joined: true },
  { kind: "ai", name: "诺里", joined: true },
  { kind: "ai", name: "蕾依（Raiy）", joined: true },
];

const TRICK_CARDS = ["S10", "HJ", "SQ", "DK", "CA"];

function playView(over = {}) {
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
    turn: 1,
    trick: { seat: 2, cards: TRICK_CARDS, combo: { type: "straight" }, name: "顺子 10-A", n: 5 },
    latest: { seat: 2, cards: TRICK_CARDS, combo: { type: "straight" }, name: "顺子 10-A", n: 5 },
    recent: [{ seat: 1, cards: null, combo: null, name: "不要", n: 0 }],
    bombs: 0,
    multiplier: 3,
    scores: [0, 0, 0],
    results: [],
    myTurn: false,
    legalBids: [],
    hints: [],
    canPass: false,
    ...over,
  };
}

function myPlayView(over = {}) {
  return playView({ turn: 0, myTurn: true, canPass: false, trick: null, latest: null, recent: [], ...over });
}

const MY_HINTS = [
  { cards: ["S3", "H3", "C3", "S4"], combo: { type: "triple1" }, move: "3 3 3 4", name: "三带一 3" },
  { cards: ["S4", "D4"], combo: { type: "pair" }, move: "4 4", name: "对 4" },
  { cards: ["S5"], combo: { type: "single" }, move: "5", name: "单 5" },
];

/* ---------------- 牌面 ---------------- */

test("牌 ID：S3/H10/DA/C2/BJ/RJ 解析为可读牌面；非法值不猜", () => {
  assert.deepEqual(parseCard("S3"), {
    id: "S3", suit: "S", suitSymbol: "♠", suitName: "黑桃",
    rank: "3", rankText: "3", label: "♠3", red: false, joker: false, order: 3,
  });
  assert.equal(parseCard("H10").label, "♥10");
  assert.equal(parseCard("DA").label, "♦A");
  assert.equal(parseCard("C2").label, "♣2");
  assert.equal(parseCard("BJ").label, "小王");
  assert.equal(parseCard("RJ").label, "大王");
  assert.equal(parseCard("RJ").red, true, "大王按红牌显示");
  assert.equal(parseCard("BJ").red, false);
  assert.equal(parseCard("S1"), null);
  assert.equal(parseCard("X3"), null);
  assert.equal(parseCard(""), null);
  assert.equal(parseCard(null), null);
  assert.equal(cardLabel("H10"), "♥10");
  assert.equal(cardLabel("junk"), "junk", "脏值原样安全展示");
  assert.equal(cardMoveText("S3"), "3");
  assert.equal(cardMoveText("H10"), "10");
  assert.equal(cardMoveText("BJ"), "BJ");
  assert.equal(cardMoveText("RJ"), "RJ");
});

test("手牌排序：点数升序、同点 ♠♥♣♦，王最大；不改入参", () => {
  const input = ["RJ", "S3", "H3", "BJ", "H10", "DA", "C2", "S4"];
  const sorted = sortCards(input);
  assert.deepEqual(sorted, ["S3", "H3", "S4", "H10", "DA", "C2", "BJ", "RJ"]);
  assert.deepEqual(input, ["RJ", "S3", "H3", "BJ", "H10", "DA", "C2", "S4"], "入参不被修改");
  assert.deepEqual(sortCards(["junk", null, "D4"]), ["D4"]);
});

test("手牌分行：20 张按 10/10，17 张按 10/7；窄屏可多行折叠", () => {
  const twenty = HAND.concat(["H4", "C4", "D5"]);
  assert.deepEqual(handRows(twenty, 10).map((r) => r.length), [10, 10]);
  assert.deepEqual(handRows(HAND, 10).map((r) => r.length), [10, 7]);
  assert.deepEqual(handRows(HAND, 0).map((r) => r.length), [10, 7], "非法行宽回落 10");
  assert.deepEqual(handRows([]), []);
});

test("点选 → move：点数升序空格串；王保持 BJ/RJ；空选择为空串", () => {
  assert.equal(selectionMove(["S4", "C3", "H3", "S3"]), "3 3 3 4");
  assert.equal(selectionMove(["RJ", "BJ"]), "BJ RJ");
  assert.equal(selectionMove(["H10", "SJ", "DQ", "CK", "HA"]), "10 J Q K A");
  assert.equal(selectionMove([]), "");
  assert.equal(selectionMove(["junk"]), "");
  const sel = toggleSelected(new Set(), "S3");
  assert.deepEqual([...sel], ["S3"]);
  assert.deepEqual([...toggleSelected(sel, "S3")], []);
  const pruned = pruneSelection(new Set(["S3", "RJ", "H2"]), ["S3", "RJ"]);
  assert.deepEqual([...pruned].sort(), ["RJ", "S3"]);
});

/* ---------------- 视角 / 回合 ---------------- */

test("视角：view.you 显式 null=观战；缺失才回退座位号", () => {
  assert.equal(youOf({ you: 0 }, { me: 2 }), 0);
  assert.equal(youOf({ you: null }, { me: 0 }), null, "显式 null 不被 me 翻案");
  assert.equal(youOf({}, { me: 1 }), 1);
  assert.equal(youOf({}, {}), null);
  assert.equal(isSpectator({ you: null }, { me: 0 }), true);
  assert.equal(isSpectator({ you: 2 }, { me: 0 }), false);
});

test("回合：view.myTurn 优先；缺失按 turn/you 回填；观战恒 false", () => {
  assert.equal(myTurnOf(myPlayView(), 0), true);
  assert.equal(myTurnOf(playView({ myTurn: true, turn: 2 }), 0), true, "myTurn 是服务端唯一真相");
  assert.equal(myTurnOf(playView({ myTurn: false, turn: 0 }), 0), false);
  assert.equal(myTurnOf({ turn: 0 }, 0), true);
  assert.equal(myTurnOf({ turn: 2 }, 0), false);
  assert.equal(myTurnOf({ turn: 0 }, null), false);
  assert.equal(phaseOf({ phase: "bid" }), "bid");
  assert.equal(phaseOf({ phase: "over" }), "over");
  assert.equal(phaseOf({ phase: "junk" }), "");
});

/* ---------------- 叫分 ---------------- */

test("叫分：只认 legalBids；0=不叫→pass；非法值丢弃", () => {
  const bid = { phase: "bid", legalBids: [3, 0, "2", 4, -1, "junk", 2], myTurn: true };
  assert.deepEqual(legalBidChoices(bid), [0, 2, 3]);
  assert.deepEqual(legalBidChoices({ phase: "play", legalBids: [1] }), []);
  assert.deepEqual(legalBidChoices({ phase: "bid" }), []);
  assert.equal(bidLabel(0), "不叫");
  assert.equal(bidLabel(2), "2 分");
  assert.equal(bidMove(0), "pass");
  assert.equal(bidMove(3), "3");
  assert.equal(bidMove(4), "");
  assert.equal(bidMove("junk"), "");
});

/* ---------------- 提示 / 闸门 ---------------- */

test("hints：服务端唯一来源；脏条目丢弃；点选优先 cards，无牌不能猜", () => {
  const view = { phase: "play", hints: MY_HINTS.concat([{ name: "空" }, null, { cards: [], move: "" }]) };
  const hints = hintsOf(view);
  assert.equal(hints.length, 3);
  assert.equal(hints[0].name, "三带一 3");
  assert.equal(hintsOf({ hints: [] }).length, 0);
  assert.equal(hintsOf(null).length, 0);
  assert.deepEqual([...hintSelection(hints[0], HAND)].sort(), ["C3", "H3", "S3", "S4"]);
  assert.deepEqual([...hintSelection({ cards: ["S9"] }, HAND)], [], "不在手里的提示牌不选中");
  assert.deepEqual([...hintSelection({ move: "9" }, HAND)], [], "只有 move 的提示无法点选（调用方直接提交）");
  assert.equal(nextHintIndex(-1, 3), 0);
  assert.equal(nextHintIndex(0, 3), 1);
  assert.equal(nextHintIndex(2, 3), 0);
  assert.equal(nextHintIndex(0, 0), -1);
});

test("操作闸门：出牌需本人回合+已选牌+未忙+未终局；不要只在 canPass 显示", () => {
  const my = myPlayView();
  assert.equal(canSubmitPlay(my, new Set(["S3"]), { canAct: true, busy: false, you: 0 }), true);
  assert.equal(canSubmitPlay(my, new Set(), { canAct: true, busy: false, you: 0 }), false, "空选择不可出牌");
  assert.equal(canSubmitPlay(my, new Set(["S3"]), { canAct: false, busy: false, you: 0 }), false, "代打/观战无操作权");
  assert.equal(canSubmitPlay(my, new Set(["S3"]), { canAct: true, busy: true, you: 0 }), false);
  assert.equal(canSubmitPlay(playView({ myTurn: false }), new Set(["S3"]), { canAct: true, busy: false, you: 0 }), false);
  assert.equal(canSubmitPlay({ ...my, phase: "over" }, new Set(["S3"]), { canAct: true, busy: false, you: 0 }), false);
  assert.equal(passVisible(myPlayView({ canPass: true }), { canAct: true, busy: false, you: 0 }), true);
  assert.equal(passVisible(myPlayView({ canPass: false }), { canAct: true, busy: false, you: 0 }), false);
  assert.equal(passVisible(playView({ canPass: true, myTurn: false }), { canAct: true, busy: false, you: 0 }), false);
});

/* ---------------- 局面文案 ---------------- */

test("阵营：地主确定前「叫分中」，确定后地主/农民；座位同类名", () => {
  assert.equal(seatRoleLabel(playView({ landlord: null, phase: "bid" }), 1), "叫分中");
  assert.equal(seatRoleLabel(playView({ landlord: null, phase: "play" }), 1), "");
  assert.equal(seatRoleLabel(playView(), 0), "地主");
  assert.equal(seatRoleLabel(playView(), 2), "农民");
  assert.equal(seatRoleClass(playView(), 0), "landlord");
  assert.equal(seatRoleClass(playView(), 1), "farmer");
  assert.equal(seatRoleClass(playView({ landlord: null, phase: "bid" }), 1), "bidding");
  assert.equal(countOf(playView(), 1), 17);
  assert.equal(countOf({ counts: [1, "x"] }, 1), null);
});

test("底牌：地主确定前 null → 牌背；公开后按数组展示；倍数/局数文案", () => {
  assert.deepEqual(bottomCards(playView()), ["S2", "BJ", "RJ"]);
  assert.equal(isBottomHidden(playView({ bottom: null })), true);
  assert.deepEqual(bottomCards(playView({ bottom: null })), []);
  assert.equal(isBottomHidden(playView({ bottom: ["junk"] })), true);
  assert.equal(multiplierText(playView({ multiplier: 6 })), "6 倍");
  assert.equal(multiplierText(playView({ multiplier: 0, bombs: 2 })), "4 倍");
  assert.equal(multiplierText(playView({ multiplier: null, bombs: 0 })), "1 倍");
  assert.equal(handNoText(playView({ handNo: 2, hands: 3 })), "第 2/3 局");
  assert.equal(handNoText(playView({ handNo: 1, hands: 1 })), "第 1 局");
  assert.equal(handNoText(playView({ handNo: 2, hands: 1 })), "第 2 局", "单局局数也如实展示当前是第几局");
});

test("桌面：要压的牌、最近动作（最新在前）、过牌动作", () => {
  const info = trickInfo(playView());
  assert.equal(info.name, "顺子 10-A");
  assert.equal(info.seat, 2);
  assert.equal(info.cards.length, 5);
  assert.equal(trickInfo(playView({ trick: { cards: null, name: "" } })), null);
  assert.equal(needsText(playView({ myTurn: false }), 0), "需要压：顺子 10-A");
  assert.equal(needsText(myPlayView(), 0), "你先出牌");
  assert.equal(needsText({ phase: "bid", myTurn: true }, 0), "轮到你叫分");
  assert.equal(needsText({ phase: "over" }, 0), "本局结束");

  const rows = recentActions(
    playView({
      recent: [
        { seat: 0, cards: ["S3"], name: "单 3", n: 1 },
        { seat: 1, cards: null, name: "不要", n: 0 },
        { seat: 2, cards: ["S4", "H4"], name: "对 4", n: 0 },
      ],
    })
  );
  assert.deepEqual(rows.map((r) => r.seat), [2, 1, 0], "最新在前");
  assert.equal(isPassAction(rows[1]), true);
  assert.equal(actionCards(rows[1]).length, 0);
  assert.equal(rows[0].name, "对 4");
  assert.deepEqual(recentActions(playView({ recent: [] })).map((r) => r.seat), [2], "缺失 recent 时回落单条 latest");
  assert.deepEqual(
    recentActions(playView({ recent: [], latest: [null, { seat: 1, cards: null, name: "不要" }, null] })),
    [],
    "latest 若为按座位的数组，不臆造时间顺序"
  );
  assert.deepEqual(recentActions({}), []);
});

test("结算：最后一局结果、春天/反春/炸弹标签、分数与增减分", () => {
  const results = [
    { hand: 1, landlordWon: false, bid: 2, bombs: 0, spring: false, antiSpring: false, deltas: [-4, 2, 2] },
    { hand: 2, landlordWon: true, bid: 3, bombs: 1, spring: true, antiSpring: false, deltas: [12, -6, -6] },
  ];
  const view = playView({ phase: "over", results, scores: [8, -4, -4] });
  const last = lastResult(view);
  assert.equal(last.hand, 2);
  assert.equal(resultTitle(last), "地主胜");
  assert.deepEqual(resultTags(last), ["春天", "炸弹 ×1", "底分 3"]);
  assert.deepEqual(scoresOf(view), [8, -4, -4]);
  assert.deepEqual(deltasOf(last), [12, -6, -6]);
  assert.equal(resultTitle(null), "本局结束");
  assert.deepEqual(resultTags(null), []);
});

test("历史 move 文案：bid/pass/牌串转人话；未知文本原样", () => {
  assert.equal(moveText("bid 2"), "叫 2 分");
  assert.equal(moveText("bid 0"), "不叫");
  assert.equal(moveText("pass"), "过");
  assert.equal(moveText("不要"), "不要");
  assert.equal(moveText("S3 H3 C3 S4"), "3 3 3 4");
  assert.equal(moveText("BJ RJ"), "小王 大王");
  assert.equal(moveText("3 3 3 4"), "3 3 3 4");
  assert.equal(moveText("resign"), "resign", "认输由页面统一翻译");
  assert.equal(moveText(""), "");
});

/* ---------------- 三人桌模式契约 ---------------- */

test("阵容：目录选项（服务端 label 优先）+ 内置四档回落", () => {
  const listed = lineupsOf({
    lineups: [
      { value: "nori_agent", label: "诺里 + 蕾依" },
      { value: "bot_bot", label: "双陪练" },
      { value: "junk", label: "非法" },
      { value: "nori_agent", label: "重复" },
    ],
  });
  assert.deepEqual(listed, [
    { value: "nori_agent", label: "诺里 + 蕾依" },
    { value: "bot_bot", label: "双陪练" },
  ]);
  assert.deepEqual(
    lineupsOf({ lineups: [
      { value: "nori_agent", label: "诺里 + 当前agent" },
      { value: "agent_bot", label: "当前agent + 陪练（诺里队友）" },
    ] }).map((x) => x.label),
    ["诺里 + 搭档", "搭档 + 陪练（诺里队友）"],
    "目录里的技术词收敛为用户文案"
  );
  const fallback = lineupsOf({});
  assert.deepEqual(fallback.map((x) => x.value), ["nori_agent", "nori_bot", "agent_bot", "bot_bot"]);
  assert.ok(fallback.every((x) => typeof x.label === "string" && x.label.length > 0));
  assert.ok(!/AI|artificial|bot/i.test(lineupHint("nori_agent")));
});

test("开局载荷：四阵容写入 seat_drivers/nori_seat/nori_role + options.hands", () => {
  const expected = {
    nori_agent: { drivers: ["human", "turn", "agent"], seat: 1, role2: "player" },
    nori_bot: { drivers: ["human", "turn", "bot"], seat: 1, role2: "player" },
    agent_bot: { drivers: ["human", "agent", "bot"], seat: null, role2: "coach" },
    bot_bot: { drivers: ["human", "bot", "bot"], seat: null, role2: "coach" },
  };
  for (const [lineup, want] of Object.entries(expected)) {
    const payload = createMatchPayload("doudizhu", { lineup, hands: "3" });
    assert.equal(payload.kind, "doudizhu");
    assert.equal(payload.mode_options.lineup, lineup);
    assert.deepEqual(payload.mode_options.seat_drivers, want.drivers);
    assert.equal(payload.mode_options.nori_seat, want.seat);
    assert.equal(payload.mode_options.nori_role, want.role2);
    assert.equal(payload.mode_options.delegation, false);
    assert.equal("mode" in payload.mode_options, false, "创建载荷只写三人桌字段（兼容字段由后端归一化产出）");
    assert.deepEqual(payload.options, { hands: "3" });
  }
  // 默认局数由后端兜底：不传 hands 就不写 options
  assert.equal("options" in createMatchPayload("doudizhu", { lineup: "nori_agent" }), false);
  assert.deepEqual(createMatchPayload("doudizhu", { lineup: "nori_agent", hands: 6 }).options, { hands: "6" }, "数字局数归一为字符串");
  assert.equal("options" in createMatchPayload("doudizhu", { lineup: "nori_agent", hands: "  " }), false, "空白局数不写");
  assert.equal(createMatchPayload("doudizhu", { lineup: "junk" }).opponent, "ai", "未知阵容回落旧载荷");
});

test("两座位旧载荷逐字段不变（阵容键不泄漏到棋类）", () => {
  assert.deepEqual(createMatchPayload("gomoku", { mode: "nori" }), { kind: "gomoku", mode_options: { mode: "nori" } });
  assert.deepEqual(createMatchPayload("gomoku", { mode: "practice", difficulty: "advanced" }), {
    kind: "gomoku",
    mode_options: { mode: "bot", difficulty: "advanced" },
  });
  assert.deepEqual(createMatchPayload("gomoku", {}), { kind: "gomoku", opponent: "ai" });
});

test("座位驱动：A/B 诺里座位 1；C/D 诺里不占座；身份按驱动而非名字", () => {
  const build = (lineup, seats) => ({
    kind: "doudizhu",
    me: 0,
    seats,
    mode_options: createMatchPayload("doudizhu", { lineup }).mode_options,
  });
  const a = build("nori_agent", SEATS);
  assert.equal(lineupOf(a), "nori_agent");
  assert.equal(noriSeatOf(a), 1);
  assert.equal(noriRoleOf(a), "player");
  assert.equal(seatDriverOf(a, 1), "turn");
  assert.equal(seatDriverOf(a, 2), "agent");
  assert.equal(seatDriverOf(a, 0), "human", "全 3 席对齐：人类座位也写明");
  assert.equal(seatIdentity(a, SEATS[1], 1), "nori");
  assert.equal(seatIdentity(a, SEATS[2], 2), "agent");
  assert.equal(isCoachMatch(a), false);

  const c = build("agent_bot", [
    { kind: "human", name: "阿空" },
    { kind: "ai", name: "蕾依（Raiy）" },
    { kind: "bot", name: "小白" },
  ]);
  assert.equal(noriSeatOf(c), null, "教练阵容诺里不占席");
  assert.equal(noriRoleOf(c), "coach");
  assert.equal(seatDriverOf(c, 1), "agent");
  assert.equal(seatDriverOf(c, 2), "bot");
  assert.equal(seatIdentity(c, { kind: "ai" }, 1), "agent");
  assert.equal(seatIdentity(c, { kind: "bot" }, 2), "bot");
  assert.equal(seatIdentityLabel(c, { kind: "ai" }, 1), "对手");
  assert.equal(isCoachMatch(c), true);
});

test("seat_drivers 全 3 席优先于 lineup 派生物；旧两项数组仍可读；脏值回落", () => {
  const match = {
    kind: "doudizhu",
    mode_options: { lineup: "nori_agent", seat_drivers: ["human", "bot", "agent"], nori_seat: 2, nori_role: "player" },
  };
  assert.equal(seatDriverOf(match, 0), "human");
  assert.equal(seatDriverOf(match, 1), "bot", "全 3 席按座位下标读取");
  assert.equal(seatDriverOf(match, 2), "agent");
  assert.equal(noriSeatOf(match), 2);
  // 旧计划的两项数组（仅座位 1..n-1）仍可读
  const legacy = { kind: "doudizhu", mode_options: { lineup: "nori_agent", seat_drivers: ["bot", "agent"] } };
  assert.equal(seatDriverOf(legacy, 1), "bot");
  assert.equal(seatDriverOf(legacy, 2), "agent");
  const dirty = { kind: "doudizhu", mode_options: { lineup: "nori_agent", seat_drivers: ["junk", null] } };
  assert.equal(seatDriverOf(dirty, 1), "turn", "脏驱动值回落到阵容计划");
  assert.equal(seatDriverOf(dirty, 2), "agent");
});

test("代打：C/D 教练阵容可代打；A/B 诺里参战不可", () => {
  const make = (lineup) => ({
    id: "dz",
    kind: "doudizhu",
    me: 0,
    seats: [{ kind: "human", name: "阿空" }, { kind: "ai" }, { kind: "bot" }],
    mode_options: createMatchPayload("doudizhu", { lineup }).mode_options,
    status: {},
    view: { phase: "play" },
  });
  assert.equal(canDelegate(make("agent_bot")), true);
  assert.equal(canDelegate(make("bot_bot")), true);
  assert.equal(canDelegate(make("nori_agent")), false);
  assert.equal(canDelegate({ ...make("agent_bot"), me: null }), false, "观战不可代打");
  assert.equal(canDelegate({ ...make("agent_bot"), status: { outcome: "win" } }), false, "终局不可代打");
  assert.deepEqual(delegatedTurnStatus({ mine: true, delegated: true, myTurn: true, act: "代打" }), {
    kind: "delegated",
    text: "诺里代打中…",
  });
  assert.equal(delegatedTurnStatus({ mine: true, delegated: false, myTurn: true, act: "代打" }), null);
});

test("终局/重开：phase=over 判终局；重开沿用阵容与局数", () => {
  assert.equal(matchOver({ status: {}, view: { phase: "over" } }), true);
  assert.equal(matchOver({ status: {}, view: { phase: "play" } }), false);
  const view = {
    kind: "doudizhu",
    seats: SEATS,
    options: { hands: "6" },
    mode_options: createMatchPayload("doudizhu", { lineup: "agent_bot" }).mode_options,
    view: { phase: "over", hands: 6 },
  };
  assert.deepEqual(restartPayload(view), {
    kind: "doudizhu",
    mode_options: createMatchPayload("doudizhu", { lineup: "agent_bot" }).mode_options,
    options: { hands: "6" },
  });
});

/* ---------------- 模块 / registry ---------------- */

test("模块契约：目录式 doudizhu 与黑白棋同形；文案不出现技术词", async () => {
  assert.equal(doudizhu.kind, "doudizhu");
  assert.equal(typeof doudizhu.mount, "function");
  assert.equal(typeof doudizhu.moveHint, "string");
  assert.ok(!/AI|artificial|bot/i.test(doudizhu.moveHint));
  assert.ok(doudizhu.moveHint.includes("提示"));
  const mod = await loadGameModule("doudizhu");
  assert.ok(mod && mod.default && mod.default.kind === "doudizhu");
  assert.equal(await loadGameModule("doudizhu_not_exist"), null);
});

test("registry：目录式 doudizhu；封面候选在本游戏目录；无素材时页面不塌", () => {
  assert.equal(DIR_MODULES.has("doudizhu"), true);
  assert.match(moduleUrls("doudizhu")[0], /\/games\/doudizhu\/index\.js$/);
  assert.match(moduleUrls("doudizhu")[1], /\/games\/doudizhu\.js$/);
  assert.deepEqual(cardCoverCandidates("doudizhu"), [
    "./games/doudizhu/assets/card-doudizhu.png",
    "./games/doudizhu/assets/card-doudizhu.svg",
    "./assets/card-doudizhu.png",
  ]);
});