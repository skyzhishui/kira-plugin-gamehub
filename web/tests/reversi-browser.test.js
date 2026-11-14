/* tests/reversi-browser.test.js — 黑白棋 DOM/交互定向测试（node:test，零依赖）。
 *
 * 与 reversi.test.js 的分工：那边测纯逻辑；这里用最小 DOM 桩驱动真实模块
 * （games/reversi/index.js → board.js），覆盖 canvas 生命周期与交互闸门：
 * 1) 挂载：8×8 棋盘、初始 4 个合法点、计数条出现且高度固定；
 * 2) 落子：点击合法点 → onPlace({x,y,move:"D6"})；点非法格不提交；
 * 3) 对手回合/忙碌/终局/观战：不绘制可落点、不接交互；
 * 4) 动效去重：同一局面重放不重播；新一手触发翻子动画；reduced-motion 不崩；
 * 5) 销毁：canvas 移除、计数条复位（destroy 幂等）。
 *
 * 运行：node --test web/tests/
 */
import test from "node:test";
import assert from "node:assert/strict";

/* ---------------- 最小 DOM 桩（只实现模块用到的 API） ---------------- */

const REDUCED = { value: false };

class FakeCanvas {
  constructor() {
    this.dataset = {};
    this.attributes = {};
    this.style = {};
    this.listeners = new Map();
    this.clientWidth = 320;
    this.clientHeight = 320;
    this.clientLeft = 6;
    this.clientTop = 6;
    this.width = 320;
    this.height = 320;
    this.className = "";
    this.tabIndex = -1;
    this.parent = null;
    this.ctx = fakeContext();
  }
  getContext() {
    return this.ctx;
  }
  addEventListener(type, fn) {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set());
    this.listeners.get(type).add(fn);
  }
  removeEventListener(type, fn) {
    this.listeners.get(type)?.delete(fn);
  }
  dispatch(type, event = {}) {
    for (const fn of this.listeners.get(type) || []) fn(event);
  }
  setAttribute(name, value) {
    this.attributes[name] = String(value);
  }
  getAttribute(name) {
    return this.attributes[name] ?? null;
  }
  getBoundingClientRect() {
    return { left: 0, top: 0, width: this.clientWidth, height: this.clientHeight };
  }
  remove() {
    if (this.parent) this.parent.children = this.parent.children.filter((c) => c !== this);
    this.parent = null;
  }
}

class FakeHost {
  constructor(width = 320, height = 320) {
    this.children = [];
    this.width = width;
    this.height = height;
  }
  appendChild(node) {
    node.parent = this;
    this.children.push(node);
    return node;
  }
  getBoundingClientRect() {
    return { left: 0, top: 0, width: this.width, height: this.height };
  }
}

function fakeContext() {
  const noop = () => {};
  const grad = { addColorStop: noop };
  return new Proxy(
    {
      canvas: null,
      createRadialGradient: () => grad,
      createLinearGradient: () => grad,
      measureText: () => ({ width: 10 }),
    },
    {
      get(target, key) {
        if (key in target) return target[key];
        return noop; // 绘制 API 一律空实现
      },
      set(target, key, value) {
        target[key] = value;
        return true;
      },
    }
  );
}

function installDom() {
  const metaSlot = {
    className: "",
    classList: {
      hidden: true,
      add(c) {
        if (c === "hidden") this.hidden = true;
      },
      remove(c) {
        if (c === "hidden") this.hidden = false;
      },
      toggle(c, on) {
        if (c === "hidden") this.hidden = !!on;
      },
      contains(c) {
        return c === "hidden" ? this.hidden : false;
      },
    },
    textContent: "",
    dataset: {},
    children: [],
    appendChild(node) {
      this.children.push(node);
      return node;
    },
  };
  const g = globalThis;
  let rafQueue = [];
  const prev = {
    document: g.document,
    window: g.window,
    getComputedStyle: g.getComputedStyle,
    ResizeObserver: g.ResizeObserver,
    requestAnimationFrame: g.requestAnimationFrame,
    cancelAnimationFrame: g.cancelAnimationFrame,
    performance: g.performance,
    PointerEvent: g.PointerEvent,
  };
  g.document = {
    createElement(tag) {
      if (tag === "canvas") return new FakeCanvas();
      return {
        tagName: String(tag).toUpperCase(),
        className: "",
        textContent: "",
        children: [],
        dataset: {},
        classList: {
          add() {},
          remove() {},
          toggle() {},
          contains() {
            return false;
          },
        },
        setAttribute(name, value) {
          this.attributes = this.attributes || {};
          this.attributes[name] = String(value);
        },
        getAttribute(name) {
          return (this.attributes && this.attributes[name]) ?? null;
        },
        appendChild(node) {
          this.children.push(node);
          return node;
        },
      };
    },
    activeElement: null,
    createTextNode(text) {
      return { nodeType: 3, textContent: String(text) };
    },
  };
  g.window = {
    devicePixelRatio: 2,
    matchMedia: () => ({
      matches: REDUCED.value,
      addEventListener() {},
      removeEventListener() {},
    }),
  };
  g.getComputedStyle = () => ({ getPropertyValue: () => "" });
  let resizeCallback;
  g.ResizeObserver = class {
    constructor(callback) { this.callback = callback; }
    observe() { resizeCallback = this.callback; }
    disconnect() {}
  };
  g.requestAnimationFrame = (cb) => {
    rafQueue.push(cb);
    return rafQueue.length;
  };
  g.cancelAnimationFrame = (id) => {
    if (id > 0) rafQueue[id - 1] = null;
  };
  g.performance = g.performance || { now: () => Date.now() };
  return {
    metaSlot,
    resize: () => resizeCallback(),
    // 只执行队首一帧（初次测量）：rAF 动画循环会自我续帧，全量 flush 会死循环
    flushOnce: () => {
      const cb = rafQueue.shift();
      if (typeof cb === "function") cb(Date.now());
    },
    restore: () => {
      rafQueue = [];
      Object.assign(g, prev);
    },
  };
}

/* ---------------- fixture ---------------- */

function initialView(over = {}) {
  const cells = new Array(64).fill(0);
  cells[27] = 2;
  cells[28] = 1;
  cells[35] = 1;
  cells[36] = 2;
  return {
    size: 8,
    cells,
    toPlay: 1,
    moves: 0,
    flipped: [],
    legal: { 19: 1, 26: 1, 37: 1, 44: 1 },
    you: 1,
    ...over,
  };
}

const MATCH = (view) => ({ id: "m1", kind: "reversi", me: 0, view });

function mountReversi() {
  const dom = installDom();
  return import("../games/reversi/index.js").then((mod) => {
    const host = new FakeHost();
    const places = [];
    const ctl = mod.default.mount(host, {
      onPlace: (p) => places.push(p),
      meta: dom.metaSlot,
    });
    dom.flushOnce(); // 初始尺寸测量（rAF）
    const canvas = host.children.find((c) => c.className.includes("board-canvas"));
    return { mod, host, canvas, places, ctl, dom };
  });
}

/** 点击画布第 index 格（用数据集几何换算内容盒坐标）。 */
function clickCell(canvas, index) {
  const [x0, y0, cell] = canvas.dataset.geom.split(",").map(Number);
  const size = Number(canvas.dataset.size);
  const x = x0 + ((index % size) + 0.5) * cell;
  const y = y0 + (Math.floor(index / size) + 0.5) * cell;
  canvas.dispatch("click", { clientX: x + canvas.clientLeft, clientY: y + canvas.clientTop });
}

/* ---------------- 用例 ---------------- */

test("挂载：8×8 棋盘 + 初始 4 个合法点 + 计数条出现（高度结构固定）", async () => {
  const { canvas, ctl, dom } = await mountReversi();
  try {
    ctl.update(MATCH(initialView()), { canAct: true, busy: false, over: false });
    assert.equal(canvas.dataset.kind, "reversi");
    assert.equal(canvas.dataset.size, "8");
    assert.equal(canvas.dataset.legal, "19,26,37,44");
    assert.equal(canvas.dataset.legalCount, "4");
    assert.equal(canvas.dataset.toPlay, "1");
    assert.equal(canvas.dataset.moves, "0");
    assert.equal(canvas.dataset.you, "1");
    assert.equal(canvas.dataset.canPlay, "1");
    assert.equal(canvas.dataset.over, "0");
    assert.ok(canvas.dataset.geom, "几何暴露给 E2E 点击换算");
    assert.equal(dom.metaSlot.classList.contains("hidden"), false);
    assert.equal(dom.metaSlot.className, "board-meta rv-meta");
    // 计数条：执色 + 双方子数 + 手数 + 说明行（两行固定结构）
    const row = dom.metaSlot.children[0];
    const note = dom.metaSlot.children[1];
    assert.equal(row.children[0].textContent, "你执黑");
    assert.equal(row.children[3].textContent, "尚未落子");
    assert.equal(note.textContent, "");
    assert.equal(note.getAttribute("role"), "status");
    assert.equal(dom.metaSlot.dataset.moves, "0");
    assert.equal(dom.metaSlot.dataset.you, "1");
    assert.ok(canvas.getAttribute("aria-label").includes("可落点 4 个"));
  } finally {
    dom.restore();
  }
});

test("落子：点击合法点提交 {x,y,move}；点非法格/已有子格不提交", async () => {
  const { canvas, places, ctl, dom } = await mountReversi();
  try {
    ctl.update(MATCH(initialView()), { canAct: true, busy: false, over: false });
    clickCell(canvas, 19); // D6
    assert.deepEqual(places, [{ x: 3, y: 2, move: "D6" }]);
    // 连点去重（350ms 内不重复提交）
    clickCell(canvas, 19);
    assert.equal(places.length, 1);
    clickCell(canvas, 27); // 已有子
    clickCell(canvas, 0); // 非合法空点
    assert.equal(places.length, 1);
  } finally {
    dom.restore();
  }
});

test("闸门：对手回合 / busy / 终局 / 观战都不画可落点、不接交互", async () => {
  const { canvas, places, ctl, dom } = await mountReversi();
  try {
    // 对手回合（toPlay=2，you=1）
    ctl.update(MATCH(initialView({ toPlay: 2, legal: { 20: 1 } })), { canAct: true, busy: false, over: false });
    assert.equal(canvas.dataset.canPlay, "0");
    clickCell(canvas, 20);
    assert.equal(places.length, 0);

    // 本方回合但动作在途
    ctl.update(MATCH(initialView()), { canAct: true, busy: true, over: false });
    assert.equal(canvas.dataset.canPlay, "0");
    clickCell(canvas, 19);
    assert.equal(places.length, 0);

    // 观战（you=null，服务端给空 legal）
    ctl.update(MATCH(initialView({ you: null, legal: {} })), { canAct: false, busy: false, over: false });
    assert.equal(canvas.dataset.you, "");
    assert.equal(canvas.dataset.legalCount, "0");
    assert.ok(canvas.getAttribute("aria-label").includes("观战"));
    assert.equal(dom.metaSlot.children.length > 0, true);

    // 终局
    ctl.update(MATCH(initialView({ over: true, legal: {}, you: 1 })), { canAct: true, busy: false, over: true });
    assert.equal(canvas.dataset.over, "1");
    assert.equal(canvas.dataset.canPlay, "0");
    assert.ok(canvas.getAttribute("aria-label").includes("对局结束"));
    clickCell(canvas, 19);
    assert.equal(places.length, 0);
  } finally {
    dom.restore();
  }
});

test("动效：同一局面重放不重播；新一手触发翻子；reduced-motion 静态不崩", async () => {
  const { canvas, ctl, dom } = await mountReversi();
  try {
    ctl.update(MATCH(initialView()), { canAct: true, busy: false, over: false });
    const after = initialView({ moves: 1, last: 19, flipped: [27], toPlay: 2, legal: { 20: 1 } });
    const cells = after.cells.slice();
    cells[19] = 1;
    cells[27] = 1;
    const played = { ...after, cells };
    ctl.update(MATCH(played), { canAct: true, busy: false, over: false });
    assert.equal(canvas.dataset.moves, "1");
    assert.equal(canvas.dataset.last, "19");
    assert.equal(canvas.dataset.animating, "1", "新一手触发翻子/落子动效");
    // 同一局面重放（聊天帧/代下状态帧）不得报错、不得重播动效
    ctl.update(MATCH(played), { canAct: false, busy: false, over: false });
    assert.equal(canvas.dataset.moves, "1");
    assert.equal(canvas.dataset.animating, "0");
    // reduced-motion：切换后仍可更新与绘制
    REDUCED.value = true;
    ctl.update(MATCH(initialView({ moves: 2, last: 26, flipped: [27] })), { canAct: true, busy: false, over: false });
    assert.equal(canvas.dataset.moves, "2");
    assert.equal(canvas.dataset.last, "26");
  } finally {
    REDUCED.value = false;
    dom.restore();
  }
});

test("键盘：方向键移动光标，回车在合法点落子；对手回合不接键盘", async () => {
  const { canvas, places, ctl, dom } = await mountReversi();
  try {
    ctl.update(MATCH(initialView()), { canAct: true, busy: false, over: false });
    globalThis.document.activeElement = canvas;
    const key = (k) => canvas.dispatch("keydown", { key: k, preventDefault() {} });
    // 光标从最后一手（无）→ 棋盘中心 (3,3)=27；移到 D6(3,2) 再回车
    key("ArrowUp");
    key("Enter");
    assert.deepEqual(places, [{ x: 3, y: 2, move: "D6" }]);
    key("Enter");
    key(" ");
    assert.equal(places.length, 1, "回车与空格共用350ms提交去重，不重复请求");
    assert.ok(canvas.getAttribute("aria-label").includes("光标 D6，"));
    key("ArrowRight");
    assert.ok(canvas.getAttribute("aria-label").includes("光标 E6，"));
    // 对手回合：键盘不落子
    ctl.update(MATCH(initialView({ toPlay: 2, legal: { 20: 1 } })), { canAct: true, busy: false, over: false });
    key("ArrowRight");
    key("Enter");
    assert.equal(places.length, 1);
    globalThis.document.activeElement = null;
  } finally {
    dom.restore();
  }
});

test("DPR 改变但 CSS 尺寸不变时重新分配画布像素", async () => {
  const { canvas, ctl, dom } = await mountReversi();
  try {
    ctl.update(MATCH(initialView()), { canAct: true, busy: false, over: false });
    assert.equal(canvas.width, 640);
    globalThis.window.devicePixelRatio = 3;
    dom.resize();
    assert.equal(canvas.width, 960);
    assert.equal(canvas.height, 960);
    globalThis.window.devicePixelRatio = 1;
    dom.resize();
    assert.equal(canvas.width, 320);
    assert.equal(canvas.height, 320);
  } finally {
    ctl.destroy();
    dom.restore();
  }
});

test("销毁：canvas 移除、计数条复位（destroy 幂等）", async () => {
  const { host, ctl, dom } = await mountReversi();
  try {
    ctl.update(MATCH(initialView()), { canAct: true, busy: false, over: false });
    assert.equal(host.children.length, 1);
    ctl.destroy();
    assert.equal(host.children.length, 0);
    assert.equal(dom.metaSlot.className, "board-meta hidden");
    ctl.destroy(); // 幂等
  } finally {
    dom.restore();
  }
});
