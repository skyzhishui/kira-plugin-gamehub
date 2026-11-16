/* tests/reversi.test.js — 黑白棋前端定向单测（node:test，零依赖、零 DOM）。
 *
 * 覆盖：
 * 1) 坐标/索引（列 A-H、行 1 在底部；初始合法点 D6/C5/F4/E3 的索引）；
 * 2) 计数、legal 归一（服务端唯一来源，脏值安全丢弃）、视角（you/观战）；
 * 3) pass 文案、最后一手文案、计数条文本（固定高度条的两行）；
 * 4) 交互闸门（本方回合 / 忙碌 / 终局 / 观战不接落子）与动效去重；
 * 5) 模块契约（目录式 games/reversi/index.js）与 registry 两种布局；
 * 6) 卡片素材落在游戏目录、用户可见文案不出现技术词。
 *
 * Run: cd web && node --test
 */
import test from "node:test";
import assert from "node:assert/strict";

import {
  COLUMNS,
  SIZE,
  boardAriaLabel,
  cellIndex,
  coordName,
  isFreshMove,
  isPlayable,
  lastMoveText,
  legalIndices,
  legalMap,
  metaText,
  normalizeSize,
  parseCoord,
  passHintText,
  playableAt,
  pointOf,
  stoneCounts,
  youOf,
} from "../games/reversi/logic.js";
import reversi from "../games/reversi/index.js";
import {
  DIR_MODULES,
  cardCoverCandidates,
  gameAssetPath,
  loadGameModule,
  moduleUrls,
} from "../games/registry.js";

/* 初始局面（与后端 engine/games/reversi/rules.py 同布局）：
 * 27=D5 白、28=E5 黑、35=D4 黑、36=E4 白；黑先，合法 D6/C5/F4/E3。
 * 索引 19=D6、26=C5、37=F4、44=E3。 */
function initialCells() {
  const cells = new Array(64).fill(0);
  cells[27] = 2;
  cells[28] = 1;
  cells[35] = 1;
  cells[36] = 2;
  return cells;
}

const INITIAL_VIEW = {
  size: 8,
  cells: initialCells(),
  toPlay: 1,
  moves: 0,
  flipped: [],
  legal: { 19: 1, 26: 1, 37: 1, 44: 1 },
  you: 1,
};

/* ---------------- 坐标 / 索引 ---------------- */

test("坐标：列 A-H 左→右，行 1 在底部；初始合法点恰为 D6/C5/F4/E3", () => {
  assert.equal(COLUMNS.slice(0, 8), "ABCDEFGH");
  assert.equal(SIZE, 8);
  assert.equal(coordName(3, 2, 8), "D6");
  assert.equal(coordName(2, 3, 8), "C5");
  assert.equal(coordName(5, 4, 8), "F4");
  assert.equal(coordName(4, 5, 8), "E3");
  assert.equal(coordName(0, 7, 8), "A1");
  assert.equal(coordName(7, 0, 8), "H8");
  const points = legalIndices(INITIAL_VIEW).map((i) => {
    const p = pointOf(i, 8);
    return coordName(p.x, p.y, 8);
  });
  assert.deepEqual(points, ["D6", "C5", "F4", "E3"]);
});

test("坐标解析：往返一致；I 列/越界/脏输入返回 null（与后端跳过 I 一致）", () => {
  for (const name of ["A1", "H8", "D6", "C5", "F4", "E3"]) {
    const p = parseCoord(name, 8);
    assert.ok(p, `${name} 应可解析`);
    assert.equal(coordName(p.x, p.y, 8), name);
  }
  assert.deepEqual(parseCoord("d6", 8), { x: 3, y: 2 });
  assert.equal(parseCoord("I6", 8), null, "列 I 不存在");
  assert.equal(parseCoord("H9", 8), null);
  assert.equal(parseCoord("D0", 8), null);
  assert.equal(parseCoord("", 8), null);
  assert.equal(parseCoord("DD6", 8), null);
  assert.equal(parseCoord(null, 8), null);
});

test("索引 <-> 点：y*size+x；越界返回 -1/null", () => {
  assert.equal(cellIndex(3, 2, 8), 19);
  assert.deepEqual(pointOf(19, 8), { x: 3, y: 2 });
  assert.equal(cellIndex(8, 0, 8), -1);
  assert.equal(cellIndex(0, -1, 8), -1);
  assert.equal(pointOf(64, 8), null);
  assert.equal(pointOf(-1, 8), null);
  assert.equal(cellIndex(1.5, 2, 8), -1);
});

test("normalizeSize：非法值回落 8，合理值保留", () => {
  assert.equal(normalizeSize(8), 8);
  assert.equal(normalizeSize("8"), 8);
  assert.equal(normalizeSize(0), 8);
  assert.equal(normalizeSize(-3), 8);
  assert.equal(normalizeSize("junk"), 8);
  assert.equal(normalizeSize(15), 15);
});

/* ---------------- 计数 / legal / 视角 ---------------- */

test("计数：按 cells 统计双方子数（不从 moves 推导）", () => {
  assert.deepEqual(stoneCounts(initialCells()), { black: 2, white: 2, empty: 60 });
  assert.deepEqual(stoneCounts([1, 1, 2, 0]), { black: 2, white: 1, empty: 1 });
  assert.deepEqual(stoneCounts(null), { black: 0, white: 0, empty: 0 });
});

test("legal 归一：字符串键/数值翻子数可读，越界与脏值丢弃（不重算规则）", () => {
  const view = {
    size: 8,
    legal: { 19: 1, "26": "2", 37: 0, 99: 3, "-1": 2, bad: 4, 44: -2, 45: "x" },
  };
  const map = legalMap(view);
  assert.deepEqual([...map.keys()].sort((a, b) => a - b), [19, 26, 37]);
  assert.equal(map.get(26), 2);
  assert.deepEqual(legalIndices(view), [19, 26, 37]);
  assert.equal(legalMap({ size: 8, legal: [] }).size, 0);
  assert.equal(legalMap(null).size, 0);
});

test("视角：view.you 优先（null=观战）；旧视图回退座位号 +1", () => {
  assert.equal(youOf({ you: 1 }, { me: 1 }), 1);
  assert.equal(youOf({ you: 2 }, {}), 2);
  assert.equal(youOf({ you: null }, { me: 0 }), 0, "显式 null 是观战，不被座位号翻案");
  assert.equal(youOf({}, { me: 0 }), 1);
  assert.equal(youOf({}, { me: 1 }), 2);
  assert.equal(youOf({}, { me: null }), 0);
  assert.equal(youOf({}, {}), 0);
});

/* ---------------- 文案 ---------------- */

test("pass 文案：对方的 pass / 自己被 pass / 观战按棋色，缺字段为空", () => {
  assert.equal(passHintText({ passed: 2 }, 1), "对方无子可落，已跳过一手");
  assert.equal(passHintText({ passed: 1 }, 1), "你无子可落，已自动跳过一手");
  assert.equal(passHintText({ passed: 2 }, null), "白方无子可落，已跳过一手");
  assert.equal(passHintText({ passed: 1 }, 0), "黑方无子可落，已跳过一手");
  assert.equal(passHintText({}, 1), "");
  assert.equal(passHintText({ passed: 3 }, 1), "");
});

test("最后一手文案：坐标 + 本手翻子数", () => {
  assert.equal(lastMoveText({ size: 8, last: 19, flipped: [27] }), "最后一步 D6 · 翻 1 子");
  assert.equal(lastMoveText({ size: 8, last: 44, flipped: [28, 36] }), "最后一步 E3 · 翻 2 子");
  assert.equal(lastMoveText({ size: 8, last: 44 }), "最后一步 E3");
  assert.equal(lastMoveText({ size: 8 }), "");
  assert.equal(lastMoveText(null), "");
});

test("计数条文本：两行固定内容；pass 优先于最后一手；观战/执色文案", () => {
  assert.deepEqual(metaText(INITIAL_VIEW, 1), {
    black: 2,
    white: 2,
    youText: "你执黑",
    movesText: "尚未落子",
    note: "",
  });
  const moved = { ...INITIAL_VIEW, moves: 1, last: 19, flipped: [27], toPlay: 2 };
  assert.equal(metaText(moved, 1).movesText, "第 1 手");
  assert.equal(metaText(moved, 1).note, "最后一步 D6 · 翻 1 子");
  const passed = { ...moved, passed: 2, toPlay: 1 };
  assert.equal(metaText(passed, 1).note, "对方无子可落，已跳过一手");
  // 终局不把 pass 提示留在结果页：优先最后一手
  assert.equal(
    metaText({ ...passed, over: true }, 1).note,
    "最后一步 D6 · 翻 1 子",
    "终局展示最后一手"
  );
  assert.equal(metaText(INITIAL_VIEW, 2).youText, "你执白");
  assert.equal(metaText(INITIAL_VIEW, 0).youText, "观战");
  assert.equal(metaText({ ...INITIAL_VIEW, cells: [1, 1, 1, 2], moves: 3 }, 1).black, 3);
});

test("读屏标签：尺寸/执色/轮次/合法点数/子数", () => {
  const label = boardAriaLabel(INITIAL_VIEW, 1);
  assert.ok(label.includes("8×8"));
  assert.ok(label.includes("你执黑"));
  assert.ok(label.includes("轮到黑方"));
  assert.ok(label.includes("可落点 4 个"));
  assert.ok(label.includes("黑 2 子、白 2 子"));
  assert.ok(boardAriaLabel({ ...INITIAL_VIEW, over: true, legal: {} }, 0).includes("观战"));
  assert.ok(boardAriaLabel({ ...INITIAL_VIEW, over: true, legal: {} }, 1).includes("对局结束"));
});

/* ---------------- 交互闸门 / 动效去重 ---------------- */

test("落子闸门：仅本方回合 + 合法点 + 未忙 + 未终局；观战不可落", () => {
  const on = { you: 1, canAct: true, busy: false, over: false };
  assert.equal(playableAt(INITIAL_VIEW, 19, on), true);
  assert.equal(playableAt(INITIAL_VIEW, 20, on), false, "非服务端合法点不可落");
  assert.equal(playableAt(INITIAL_VIEW, 19, { ...on, you: 2 }), false, "不是本方回合");
  assert.equal(playableAt(INITIAL_VIEW, 19, { ...on, busy: true }), false);
  assert.equal(playableAt(INITIAL_VIEW, 19, { ...on, canAct: false }), false);
  assert.equal(playableAt(INITIAL_VIEW, 19, { ...on, you: 0 }), false, "观战不接落子");
  assert.equal(playableAt({ ...INITIAL_VIEW, over: true }, 19, on), false);
  assert.equal(isPlayable(INITIAL_VIEW, on), true);
  assert.equal(isPlayable(INITIAL_VIEW, { ...on, you: 2 }), false);
  assert.equal(isPlayable({ ...INITIAL_VIEW, legal: {} }, on), false);
});

test("新一手识别：同局面/重放/首帧不重播翻子动画", () => {
  assert.equal(isFreshMove(null, INITIAL_VIEW), false, "首帧静态呈现");
  const before = { ...INITIAL_VIEW, moves: 3, last: 26 };
  const after = { ...before, moves: 4, last: 19, flipped: [27] };
  assert.equal(isFreshMove(before, after), true);
  assert.equal(isFreshMove(after, after), false, "同版本重放不重播");
  assert.equal(isFreshMove(after, { ...after, moves: 3 }), false, "回退不重播");
  assert.equal(isFreshMove(before, { ...before, passed: 2 }), false, "pass 不是新落子");
  assert.equal(isFreshMove(before, { ...before, moves: 9 }), true, "跳帧（重连）也只播一次");
});

/* ---------------- 模块契约 / registry ---------------- */

test("模块契约：目录式模块与五子棋同形（kind/moveHint/mount）", () => {
  assert.equal(reversi.kind, "reversi");
  assert.equal(typeof reversi.mount, "function");
  assert.equal(typeof reversi.moveHint, "string");
  assert.ok(!/AI|artificial|bot/i.test(reversi.moveHint));
  assert.ok(reversi.moveHint.includes("D6"));
});

test("registry：目录式 reversi 先找 games/reversi/index.js；gomoku 仍走平铺文件", () => {
  assert.equal(DIR_MODULES.has("reversi"), true);
  assert.match(moduleUrls("reversi")[0], /\/games\/reversi\/index\.js$/);
  assert.match(moduleUrls("reversi")[1], /\/games\/reversi\.js$/);
  assert.match(moduleUrls("gomoku")[0], /\/games\/gomoku\.js$/);
  assert.deepEqual(moduleUrls("../evil"), []);
  assert.deepEqual(moduleUrls(""), []);
});

test("registry 动态加载：reversi/gomoku 都能取到 default 模块；非法/未知 kind 返回 null", async () => {
  const rv = await loadGameModule("reversi");
  assert.ok(rv && rv.default && rv.default.kind === "reversi");
  const go = await loadGameModule("gomoku");
  assert.ok(go && go.default && go.default.kind === "gomoku");
  assert.equal(await loadGameModule("../etc/passwd"), null);
  assert.equal(await loadGameModule("does_not_exist"), null);
});

test("卡片素材：目录式棋种优先本游戏目录（png→svg），五子棋保持公共 assets", () => {
  assert.deepEqual(cardCoverCandidates("reversi"), [
    "./games/reversi/assets/card-reversi.png",
    "./games/reversi/assets/card-reversi.svg",
    "./assets/card-reversi.png",
  ]);
  assert.deepEqual(cardCoverCandidates("gomoku"), ["./assets/card-gomoku.png"]);
  assert.deepEqual(cardCoverCandidates("../x"), []);
  assert.equal(gameAssetPath("reversi", "card-reversi.svg"), "./games/reversi/assets/card-reversi.svg");
  assert.equal(gameAssetPath("reversi", "../secret"), "");
  assert.equal(gameAssetPath("gomoku", "card-gomoku.png"), "");
});
